# browser-relay-mcp

Unlimited, session-authenticated web **search + fetch** for your AI tools — driven through *your own browser*, behind a local [MCP](https://modelcontextprotocol.io) server.

No API keys, no per-query billing, no rate-limit walls. Searches and page fetches run in a real browser that is already logged into the sites you use, so paywalled / authenticated pages come back as clean readable text just like you'd see them.

Two interchangeable drivers sit behind one MCP surface:

- **`relay`** (default) — drives *your* logged-in Chrome via a loaded extension. The extension polls the local relay at `http://localhost:15552`, runs the search/fetch in a tab, and posts the result back. Your sessions, your cookies.
- **`cloak`** — an embedded [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) stealth browser, launched in-process by the relay. For bot-protected or unattended pages where you don't want to (or can't) use your attended Chrome. Requires `patchright install chromium` once.

Every MCP tool takes a `driver` parameter, so a caller can pick per-request.

## Architecture

```
                          ┌─────────────────────────────────────────┐
  MCP client              │  browser-relay-mcp                        │
  (Claude, STORM, …)      │                                           │
        │                 │   MCP server  (stdio / FastMCP)           │
        │  search/fetch/  │       │                                   │
        │  search_and_    │       │  HTTP                             │
        │  fetch/resume/  │       ▼                                   │
        └───────────────► │   relay  (FastAPI @ localhost:15552)      │
           tool call      │       │                                   │
                          │       ├── driver=relay ─► job queue ──────┼──┐
                          │       │                                   │  │ poll /pending
                          │       │                                   │  │ post /result
                          │       └── driver=cloak ─► patchright ─────┼─┐│
                          └─────────────────────────────────────────┘ ││
                                                                       ││
                  ┌────────────────────────────────────────────────┐  ││
                  │  embedded stealth Chromium (in-process) ◄────────┼──┘│
                  └────────────────────────────────────────────────┘   │
                  ┌────────────────────────────────────────────────┐   │
                  │  YOUR logged-in Chrome  +  Browser Relay ext ◄───┼───┘
                  │  (polls localhost:15552, runs job in a tab)      │
                  └────────────────────────────────────────────────┘
```

The MCP server auto-starts the relay backend on first use (a local FastAPI process on `127.0.0.1:15552`). The `relay` driver needs the Chrome extension loaded and pointed at that URL; the `cloak` driver needs `patchright install chromium`.

## Install

### 1. The MCP server

Run it with [uv](https://docs.astral.sh/uv/) — no checkout required:

```bash
uvx browser-relay-mcp
```

Add it to your MCP client config:

```json
{ "mcpServers": { "browser-relay": { "command": "uvx", "args": ["browser-relay-mcp"] } } }
```

### 2. The Chrome extension (for the `relay` driver)

1. Open `chrome://extensions`, enable **Developer mode**.
2. **Load unpacked** → select the `extension/` directory (or unzip a packaged build — see [Packaging](#packaging-the-extension)).
3. Open the extension's **options** and set the **Relay server URL** to `http://localhost:15552`.
4. The popup status should read **Relay OK** / **connected**.

The extension only acts on pages the relay explicitly tells it to drive — see [docs/privacy-policy.md](docs/privacy-policy.md).

### 3. The cloak browser (for the `cloak` driver)

One-time, to download the stealth Chromium build patchright uses:

```bash
patchright install chromium
```

If you only ever use `driver: "relay"`, this step is optional. When it's missing, `/health` reports the cloak driver as unavailable instead of failing your relay calls.

## Tools

All tools return a JSON string. The `driver` parameter selects which backend runs the request.

| Tool | Signature | Returns |
|------|-----------|---------|
| `search` | `search(query, k=10, engine="bing", driver="relay")` | `{status, query, engine, driver, count, results:[{title, url, snippet}]}` |
| `fetch` | `fetch(url, include_html=False, driver="relay")` | `{status, url, driver, title, text, excerpt, length[, html]}` |
| `search_and_fetch` | `search_and_fetch(query, k=5, engine="bing", driver="relay")` | `{status, query, engine, driver, count, results:[{title, url, snippet, text, length, fetch_error}]}` |
| `resume` | `resume(resume_token)` | the completed result, `action_required` again if still blocked, or `error` if the token expired |
| `health` | `health()` | relay + extension + cloak connectivity and queue depth (see below) |

`search_and_fetch` runs the search, then fetches the top-k results in parallel. A page that fails to fetch records its own `fetch_error` and `text=""`; the batch still returns. Per-result escalation inside the batch is non-interactive — a blocked result is reported as `fetch_error: "action_required: <action>"`, and you re-drive it with an individual `fetch(url)`.

### `health` shape

```json
{
  "status": "ok",
  "extension_connected": true,
  "extension_status": "connected",
  "last_poll_age_seconds": 2.1,
  "search_queued": 0,
  "fetch_queued": 0,
  "in_flight": 0,
  "max_fetch_tabs": 5,
  "engine": "bing",
  "pending_actions": [],
  "version": "0.1.0",
  "drivers": {
    "relay": { "extension_connected": true, "extension_status": "connected", "last_poll_age_seconds": 2.1 },
    "cloak": { "available": true, "profile_path": "…/browser-relay/cloak-profile", "pages_open": 0 }
  }
}
```

`extension_status` is one of `connected`, `stale` (extension was seen but hasn't polled within ~75s), or `never_seen`. The `cloak` block reports `available: false` with an `error` when patchright's browser isn't installed.

## Escalation flow (CAPTCHA / login)

When a search or fetch hits a CAPTCHA or a login wall, the call doesn't fail — it pauses and surfaces the blocked browser to you:

```
search/fetch  ──►  status: "action_required"
                   { driver, action: "solve_captcha" | "login",
                     message, resume_token, query|url }
                          │
        you solve the CAPTCHA / sign in
        in the surfaced browser window
                          │
                          ▼
        resume(resume_token)  ──►  status: "ok" (completed result)
                                   status: "action_required" (still blocked — solve again, same token)
                                   status: "error" (token expired, default TTL 300s)
```

- **`relay`**: the extension holds the tab open; you solve it in your Chrome.
- **`cloak`**: the embedded browser window is non-headless on purpose so you can solve the challenge there.

`resume` reuses the held tab/page rather than starting over, and the same `resume_token` stays valid across repeated `action_required` rounds until it succeeds or the TTL (`BROWSER_RELAY_ACTION_TTL`, default 300s) expires.

## STORM adapter

`adapters/storm.py` maps a `search_and_fetch` result into the [STORM](https://github.com/stanford-oval/storm) / dspy retriever shape, so browser-relay can back STORM's knowledge-curation retrieval:

```python
from adapters.storm import to_storm

# `result` is the parsed dict from search_and_fetch(...)
sources = to_storm(result)
# -> [{ "url", "title", "description", "snippets": [chunks of the page text] }, ...]
```

Only results that actually came back with text are included; page text is chunked (1000 chars/chunk) into `snippets`.

## Run the acceptance checks

The PRD acceptance criteria are split between a live human-attended script and unit tests.

**Live (C1–C4)** — requires Chrome with the extension loaded and signed in:

```bash
# 1. start the relay
cd server && uv run browser-relay-mcp --backend --port 15552

# 2. in Chrome: load unpacked extension/, set server URL to http://localhost:15552 (status: connected)

# 3. run the checks
uv run --with httpx python tests/acceptance/run_acceptance.py
```

Expected output ends with `ALL ACCEPTANCE CRITERIA PASSED`. See [tests/acceptance/README.md](tests/acceptance/README.md) for what each criterion verifies (C3 is the headline test: 50 sequential searches with **0 errors and 0 silent-empty** result sets).

**STORM adapter (C5)** — unit test:

```bash
cd adapters && python -m pytest tests/test_storm.py
```

**Server + cloak suites:**

```bash
cd server && uv run pytest                      # full server suite
BROWSER_RELAY_RUN_CLOAK_TESTS=1 uv run pytest   # include live cloak tests
```

**Extension unit tests:**

```bash
npm test
```

## Packaging the extension

```bash
bash scripts/package-extension.sh
# wrote dist/browser-relay-extension.zip
```

This regenerates the injected bundles (`npm run build:inject`) and zips `extension/` (tests excluded) for the Chrome Web Store or manual install. `dist/` is gitignored.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `BROWSER_RELAY_URL` | `http://127.0.0.1:15552` | Relay base URL the MCP server talks to |
| `BROWSER_RELAY_FETCH_CAP` | `5` | Max parallel fetch tabs |
| `BROWSER_RELAY_SEARCH_CONCURRENCY` | `1` | Concurrent searches (near-serial by design) |
| `BROWSER_RELAY_SEARCH_MIN_SPACING_MS` | `500` | Minimum spacing between search dispatches |
| `BROWSER_RELAY_ACTION_TTL` | `300` | Seconds a paused (action_required) request stays resumable |
| `BROWSER_RELAY_DEFAULT_ENGINE` | `bing` | Default search engine |
| `BROWSER_RELAY_CLOAK_PROFILE_DIR` | user cache dir | Persistent profile for the cloak browser |
