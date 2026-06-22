# browser-relay-mcp — Plan 3: Escalation Protocol (action_required + resume) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the human-in-the-loop escalation: when a search/fetch hits a CAPTCHA or login wall, surface the browser window, return `{status:"action_required", resume_token, ...}` through the MCP, and let `resume(resume_token)` re-check and complete the operation once the human has acted.

**Architecture:** The extension, on detecting a block (relay driver), keeps the tab open, sets it `active` (visible), and posts an `action_required` signal to `/result` instead of a result or error. The relay registers a paused **action** keyed by a `resume_token`, returns `action_required` to the still-blocked caller, and holds the tab id. `resume(resume_token)` enqueues a **recheck job** for the same tab; the extension re-parses/re-extracts that tab and posts the real result (or `action_required` again if still blocked). Paused actions expire after a TTL, closing the tab. A new `resume` MCP tool and `/resume/{token}` endpoint expose this; `health` lists `pending_actions`.

**Tech Stack:** Same as Phases 1-2 (FastAPI relay, FastMCP, Chrome MV3 extension). New: an action registry + recheck-job kind in the relay; an `escalate`/`recheck` path in `background.js`.

## Global Constraints

- **Status envelope adds `action_required`:** tools/endpoints now return `status ∈ {"ok","action_required","error"}`. `action_required` payload: `{status:"action_required", action, url, message, resume_token, driver}` where `action ∈ {"solve_captcha","login"}`.
- **Block is no longer an error (relay driver):** Phase 2 posted `{error:"blocked: bing challenge"}`. With escalation, a detected block instead posts `{action_required:true, action:"solve_captcha", tab_id:<id>}` and the relay turns it into `status:"action_required"`. (Fetch login walls likewise → `action:"login"`.) A genuine engine error still posts `{error}`.
- **Tab stays open on escalation; surfaced to the user:** the extension sets the tab `active:true` and does NOT remove it. The relay records the `tab_id`. On resume → recheck the same tab; on TTL expiry → relay enqueues a `close_tabs` entry.
- **`ACTION_TTL = 300` seconds** (env `BROWSER_RELAY_ACTION_TTL`). After expiry, the paused action is dropped, the tab is queued for close, and `resume` for that token returns `{status:"error", error:"action expired"}`.
- **Non-blocking original call:** the original `/search`/`/fetch` call RETURNS `action_required` immediately when the block signal arrives (it does not keep blocking for the human). The caller then drives `resume`.
- **`resume` is idempotent-ish:** `resume(token)` → `ok` (cleared), `action_required` (same token, still blocked), or `error` (expired/unknown). Re-resuming a completed token → `error: "action already resolved or unknown"`.
- **Backward compat:** `driver`, all Phase 1/2 shapes, and the simulated-extension test helper remain. `search_and_fetch`'s per-result handling: a result whose fetch escalates records `fetch_error:"action_required: <url>"` (non-interactive in the batch, per spec §18) — the batch still returns.
- **Fail loud:** unknown token, expired token, relay/extension down → explicit `error`. Never hang, never silent.

---

### Task 1: Relay action registry + escalation signal handling in `/result`

**Files:**
- Modify: `server/browser_relay/app.py`
- Create: `server/tests/test_escalation.py`

**Interfaces:**
- Consumes: the Job model, `_await_job`, `post_result` from Phase 1.
- Produces:
  - An `Action` record + `actions: dict[str, Action]` registry keyed by `resume_token`.
  - `/result/{job_id}` now recognizes an `action_required` body (`{"action_required": true, "action": "...", "tab_id": ...}`): it creates an `Action`, sets the job result to an `action_required` shape, and sets the event (unblocking the original caller with `action_required`).
  - `_shape_search`/`_shape_fetch` pass through an `action_required` result unchanged (status `action_required`, including `resume_token`, `action`, `message`).
  - Helper `_register_action(job, action_kind, tab_id) -> resume_token`.

- [ ] **Step 1: Write the failing test**

`server/tests/test_escalation.py`:
```python
import asyncio

import browser_relay.app as appmod
from tests.conftest import drive_extension


def setup_function():
    for d in (appmod.search_queue, appmod.fetch_queue):
        d.clear()
    appmod.jobs.clear()
    appmod.actions.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


async def test_search_block_signal_becomes_action_required(client):
    def respond(job):
        # Simulate the extension detecting a CAPTCHA: it keeps the tab open and
        # posts an action_required signal instead of results.
        return {"action_required": True, "action": "solve_captcha", "tab_id": 4242}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "action_required"
    assert body["action"] == "solve_captcha"
    assert body["driver"] == "relay"
    assert body["resume_token"]  # non-empty token issued
    assert "message" in body
    # The action is registered, holding the tab id.
    action = appmod.actions[body["resume_token"]]
    assert action.tab_id == 4242
    assert action.kind == "search"


async def test_fetch_login_signal_becomes_action_required(client):
    def respond(job):
        return {"action_required": True, "action": "login", "tab_id": 99}

    caller = client.get("/fetch", params={"url": "https://paywalled.example/article"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "action_required"
    assert body["action"] == "login"
    assert body["url"] == "https://paywalled.example/article"
    assert body["resume_token"] in appmod.actions
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_escalation.py -v`
Expected: FAIL — `AttributeError: module 'browser_relay.app' has no attribute 'actions'`.

- [ ] **Step 3: Add the Action registry + constants**

In `server/browser_relay/app.py`, add after the existing config constants (near `DEFAULT_ENGINE`):
```python
import secrets

ACTION_TTL = float(os.environ.get("BROWSER_RELAY_ACTION_TTL", "300"))

_ACTION_MESSAGES = {
    "solve_captcha": "The search engine is showing a CAPTCHA. A browser window has been "
                     "opened — please solve the challenge, then call resume with this token.",
    "login": "The page requires sign-in. A browser window has been opened — please log in, "
             "then call resume with this token.",
}


class Action:
    __slots__ = ("resume_token", "kind", "payload", "tab_id", "action", "created_at", "resolved")

    def __init__(self, kind: str, payload: dict, tab_id, action: str):
        self.resume_token = secrets.token_urlsafe(12)
        self.kind = kind            # "search" | "fetch"
        self.payload = payload      # original job payload (query/engine/k or url)
        self.tab_id = tab_id
        self.action = action        # "solve_captcha" | "login"
        self.created_at = time.monotonic()
        self.resolved = False


actions: dict[str, Action] = {}
```

- [ ] **Step 4: Add the registration helper + action_required shaping**

Add to `app.py`:
```python
def _register_action(job: "Job", action: str, tab_id) -> Action:
    record = Action(job.kind, dict(job.payload), tab_id, action)
    actions[record.resume_token] = record
    return record


def _action_required_payload(record: Action) -> dict:
    base = {
        "status": "action_required",
        "driver": "relay",
        "action": record.action,
        "message": _ACTION_MESSAGES.get(record.action, "Action required in the browser window."),
        "resume_token": record.resume_token,
    }
    if record.kind == "search":
        base["query"] = record.payload.get("query")
        base["engine"] = record.payload.get("engine")
    else:
        base["url"] = record.payload.get("url")
    return base
```

- [ ] **Step 5: Teach `_shape_search`/`_shape_fetch` + `post_result` about action_required**

Replace `_shape_search` and `_shape_fetch` so an `action_required` result (stored on the job by `post_result`) passes through. Change the early branch in each:
```python
def _shape_search(job: Job) -> dict:
    result = job.result or {"error": "no result"}
    if result.get("status") == "action_required":
        return result  # already shaped by post_result
    base = {"query": job.payload["query"], "engine": job.payload["engine"], "driver": "relay"}
    if "error" in result:
        return {"status": "error", **base, "error": result["error"]}
    results = result.get("results", [])
    return {"status": "ok", **base, "count": len(results), "results": results}


def _shape_fetch(job: Job) -> dict:
    result = job.result or {"error": "no result"}
    if result.get("status") == "action_required":
        return result
    base = {"url": job.payload["url"], "driver": "relay"}
    if "error" in result:
        return {"status": "error", **base, "error": result["error"]}
    out = {"status": "ok", **base}
    for key in ("title", "text", "excerpt", "length", "html"):
        if key in result:
            out[key] = result[key]
    return out
```

Replace `post_result` to detect the escalation signal:
```python
@app.post("/result/{job_id}")
async def post_result(job_id: str, body: ResultBody):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or expired")
    data = body.model_dump()
    if data.get("action_required"):
        record = _register_action(job, data.get("action", "solve_captcha"), data.get("tab_id"))
        job.result = _action_required_payload(record)
    else:
        job.result = data
    _release(job)
    job.event.set()
    return {"status": "ok"}
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd server && uv run pytest tests/test_escalation.py -v`
Expected: PASS (2 tests). Then full suite `cd server && uv run pytest` — Phase 1 tests still green (the `action_required` branch only triggers on the new signal).

- [ ] **Step 7: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_escalation.py
git commit -m "feat(relay): action registry + action_required escalation signal in /result"
```

---

### Task 2: `/resume/{token}` endpoint + recheck job + TTL expiry

**Files:**
- Modify: `server/browser_relay/app.py`
- Modify: `server/tests/test_escalation.py`

**Interfaces:**
- Consumes: the `actions` registry, queues, `_await_job`.
- Produces:
  - `POST /resume/{token}` → enqueues a **recheck job** (kind unchanged, payload includes `recheck_tab_id`), blocks on it like a normal job, returns the rechecked result (`ok` / `action_required` again / `error`). Unknown/expired/resolved token → `{status:"error", error}`.
  - `/pending` includes recheck jobs (a job carrying `recheck_tab_id` so the extension re-uses that tab instead of opening a new one).
  - A TTL sweep (extend the existing `_cleanup` loop, or add one) that drops expired actions and queues their tab for `close_tabs`.
  - `close_tabs` is now actually populated from an `action_close_queue`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_escalation.py`:
```python
async def test_resume_unknown_token_errors(client):
    resp = await client.post("/resume/nope-not-real")
    body = resp.json()
    assert body["status"] == "error"
    assert "expired" in body["error"] or "unknown" in body["error"]


async def test_resume_rechecks_tab_and_returns_ok(client):
    # First: a search escalates (block), registering an action with tab 7.
    def block_then_succeed(job):
        if "recheck_tab_id" in job:
            # recheck job — challenge now cleared, return real results
            return {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}
        return {"action_required": True, "action": "solve_captcha", "tab_id": 7}

    caller = client.get("/search", params={"q": "blocked-then-ok"})
    _, resp = await asyncio.gather(drive_extension(client, block_then_succeed), caller)
    token = resp.json()["resume_token"]
    assert resp.json()["status"] == "action_required"

    # Then: resume → recheck job served by the same simulated extension → ok results.
    resume_caller = client.post(f"/resume/{token}")
    _, rresp = await asyncio.gather(drive_extension(client, block_then_succeed), resume_caller)
    rbody = rresp.json()
    assert rbody["status"] == "ok"
    assert rbody["count"] == 1
    assert rbody["results"][0]["url"] == "https://e"
    # Resolved token can't be resumed again.
    again = (await client.post(f"/resume/{token}")).json()
    assert again["status"] == "error"


async def test_recheck_job_carries_tab_id_in_pending(client):
    def respond(job):
        return {"action_required": True, "action": "solve_captcha", "tab_id": 55}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    token = resp.json()["resume_token"]

    # Kick off a resume, then inspect what /pending hands the extension.
    resume_task = asyncio.create_task(client.post(f"/resume/{token}"))
    await asyncio.sleep(0.05)
    pend = (await client.get("/pending")).json()
    recheck_jobs = [j for j in pend["jobs"] if j.get("recheck_tab_id") == 55]
    assert len(recheck_jobs) == 1
    assert recheck_jobs[0]["kind"] == "search"
    resume_task.cancel()


async def test_expired_action_is_swept_and_tab_queued_for_close(client, monkeypatch):
    monkeypatch.setattr(appmod, "ACTION_TTL", 0.0)  # everything is immediately expired
    def respond(job):
        return {"action_required": True, "action": "solve_captcha", "tab_id": 321}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    token = resp.json()["resume_token"]

    await appmod._sweep_expired_actions()  # directly invoke the sweep
    assert token not in appmod.actions
    # The expired action's tab is queued for the extension to close.
    pend = (await client.get("/pending")).json()
    assert 321 in pend["close_tabs"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd server && uv run pytest tests/test_escalation.py -v`
Expected: FAIL — `/resume/...` 404 (route undefined) and `_sweep_expired_actions` missing.

- [ ] **Step 3: Add the close queue + sweep + recheck dispatch**

In `app.py`, add near the `actions` registry:
```python
from collections import deque  # already imported at top; ensure present

action_close_queue: deque = deque()  # tab ids to close (expired/resolved actions)
```

Add the sweep function:
```python
async def _sweep_expired_actions():
    now = time.monotonic()
    expired = [t for t, a in actions.items() if not a.resolved and (now - a.created_at) >= ACTION_TTL]
    for token in expired:
        record = actions.pop(token, None)
        if record and record.tab_id is not None:
            action_close_queue.append(record.tab_id)
```

If Phase 1 has no background cleanup loop, add a lifespan task; otherwise extend it. Add this lifespan (replace the bare `app = FastAPI()` if needed):
```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    async def _loop():
        while True:
            await asyncio.sleep(30)
            await _sweep_expired_actions()
    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=_lifespan)
```
(Move the `ResultBody`/middleware definitions to remain after `app` as before; only the `app = FastAPI(...)` line changes.)

- [ ] **Step 4: Populate `close_tabs` in `/pending`**

In `pending()`, replace the `close_tabs: []` with a drain of the queue. Before building the response:
```python
    close_tabs = []
    while action_close_queue:
        close_tabs.append(action_close_queue.popleft())
```
and return `{"jobs": batch, "close_tabs": close_tabs}`.

- [ ] **Step 5: Implement `/resume/{token}`**

Add to `app.py`:
```python
@app.post("/resume/{token}")
async def resume(token: str):
    record = actions.get(token)
    if not record:
        return {"status": "error", "error": "action expired or unknown"}
    if record.resolved:
        return {"status": "error", "error": "action already resolved or unknown"}
    if (time.monotonic() - record.created_at) >= ACTION_TTL:
        actions.pop(token, None)
        if record.tab_id is not None:
            action_close_queue.append(record.tab_id)
        return {"status": "error", "error": "action expired"}

    # Enqueue a recheck job that re-uses the held tab.
    payload = dict(record.payload)
    payload["recheck_tab_id"] = record.tab_id
    job = Job(record.kind, payload)
    queue = search_queue if record.kind == "search" else fetch_queue
    timeout = QUERY_TIMEOUT if record.kind == "search" else FETCH_TIMEOUT
    await _await_job(job, queue, timeout)

    result = _shape_search(job) if record.kind == "search" else _shape_fetch(job)
    if result.get("status") == "action_required":
        # Still blocked — keep the SAME token alive for another resume.
        # post_result registered a new action; collapse it back onto this token.
        new_token = result.get("resume_token")
        if new_token and new_token in actions:
            actions[token] = actions.pop(new_token)
            actions[token].resume_token = token
            actions[token].created_at = time.monotonic()  # refresh TTL on activity
            result["resume_token"] = token
    else:
        record.resolved = True
        actions.pop(token, None)
    return result
```

- [ ] **Step 6: Make recheck dispatch respect the held tab in `/pending`**

The recheck job carries `recheck_tab_id` in its payload, which flows into the `/pending` job dict via `**job.payload`. No change needed beyond Step 4 — confirm a recheck search job appears in `/pending` with `recheck_tab_id` (the test asserts this).

- [ ] **Step 7: Run to verify they pass**

Run: `cd server && uv run pytest tests/test_escalation.py -v`
Expected: PASS (6 tests total). Then full suite — all green.

- [ ] **Step 8: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_escalation.py
git commit -m "feat(relay): /resume endpoint, recheck jobs, action TTL sweep, close_tabs draining"
```

---

### Task 3: `resume` MCP tool + `health.pending_actions`

**Files:**
- Modify: `server/browser_relay/mcp_server/server.py`
- Modify: `server/browser_relay/app.py` (health adds `pending_actions`)
- Create: `server/tests/test_resume_tool.py`
- Modify: `server/tests/test_health.py`

**Interfaces:**
- Consumes: `/resume/{token}`, `/health`.
- Produces:
  - `async resume(resume_token: str) -> str` MCP tool → JSON of the resumed result.
  - `health` JSON gains `pending_actions: [{resume_token, action, url|query, driver, since_seconds}]`.

- [ ] **Step 1: Write the failing tool test**

`server/tests/test_resume_tool.py`:
```python
import asyncio
import json

import httpx
from httpx import ASGITransport

import browser_relay.app as appmod
from browser_relay.app import app
from browser_relay.mcp_server import server as mcpserver
from tests.conftest import drive_extension


def setup_function():
    for d in (appmod.search_queue, appmod.fetch_queue):
        d.clear()
    appmod.jobs.clear()
    appmod.actions.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


async def test_resume_tool_passthrough(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    drive_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    def block_then_ok(job):
        if "recheck_tab_id" in job:
            return {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}
        return {"action_required": True, "action": "solve_captcha", "tab_id": 11}

    async with drive_client:
        search_caller = mcpserver.search("q")
        _, sout = await asyncio.gather(drive_extension(drive_client, block_then_ok), search_caller)
        token = json.loads(sout)["resume_token"]

        resume_caller = mcpserver.resume(token)
        _, rout = await asyncio.gather(drive_extension(drive_client, block_then_ok), resume_caller)
    body = json.loads(rout)
    assert body["status"] == "ok"
    assert body["count"] == 1


async def test_resume_tool_unknown_token(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    out = await mcpserver.resume("bogus")
    body = json.loads(out)
    assert body["status"] == "error"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_resume_tool.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'resume'`.

- [ ] **Step 3: Add the `resume` tool**

In `server/browser_relay/mcp_server/server.py`, after `search_and_fetch`:
```python
@mcp.tool()
async def resume(resume_token: str) -> str:
    """Resume a search/fetch that paused for human action (CAPTCHA or login).

    Call this after the user has solved the challenge / logged in, using the
    resume_token from a previous action_required result. Returns the completed
    result (status "ok"), "action_required" again if still blocked, or "error"
    if the token expired.
    """
    result = await _request("POST", f"/resume/{resume_token}")
    return json.dumps(result, indent=2)
```

- [ ] **Step 4: Add `pending_actions` to `/health`**

In `app.py` `health()`, before the return, build the list and add it to the dict:
```python
    now_m = time.monotonic()
    pending_actions = [
        {
            "resume_token": a.resume_token,
            "action": a.action,
            "driver": "relay",
            "since_seconds": round(now_m - a.created_at, 1),
            **({"query": a.payload.get("query")} if a.kind == "search" else {"url": a.payload.get("url")}),
        }
        for a in actions.values()
        if not a.resolved
    ]
```
and add `"pending_actions": pending_actions,` to the returned dict.

- [ ] **Step 5: Extend the health test**

Append to `server/tests/test_health.py`:
```python
async def test_health_lists_pending_actions(client):
    appmod.actions.clear()
    from browser_relay.app import Action
    rec = Action("search", {"query": "q", "engine": "bing"}, 7, "solve_captcha")
    appmod.actions[rec.resume_token] = rec
    body = (await client.get("/health")).json()
    assert any(p["resume_token"] == rec.resume_token and p["action"] == "solve_captcha"
               for p in body["pending_actions"])
    appmod.actions.clear()
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd server && uv run pytest tests/test_resume_tool.py tests/test_health.py -v`
Expected: PASS. Then full suite — all green.

- [ ] **Step 7: Commit**

```bash
git add server/browser_relay/mcp_server/server.py server/browser_relay/app.py server/tests/test_resume_tool.py server/tests/test_health.py
git commit -m "feat(mcp): resume tool + health.pending_actions"
```

- [ ] **Step 8: Fix `search_and_fetch` per-result escalation labeling**

`_fetch_one` in `server.py` currently does `f.get("error", "fetch failed")`. An `action_required` result has no `error` key, so it would mislabel. Update `_fetch_one` to capture the escalation explicitly. Replace the `_fetch_one` body's failure branch:
```python
    async def _fetch_one(item: dict) -> dict:
        f = await _request("GET", "/fetch", params={"url": item["url"], "driver": driver})
        if f.get("status") == "ok":
            return {**item, "text": f.get("text", ""), "length": f.get("length", 0), "fetch_error": None}
        if f.get("status") == "action_required":
            # Per spec §18, per-result escalation is non-interactive inside the batch:
            # record it so the caller can fetch(url) individually to drive the handoff.
            return {**item, "text": "", "length": 0,
                    "fetch_error": f"action_required: {f.get('action', 'login')}"}
        return {**item, "text": "", "length": 0, "fetch_error": f.get("error", "fetch failed")}
```
Add a test to `server/tests/test_search_and_fetch.py`:
```python
async def test_search_and_fetch_records_action_required_as_fetch_error(monkeypatch):
    import httpx
    from httpx import ASGITransport
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    drive_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    def respond(job):
        if job["kind"] == "search":
            return {"results": [{"title": "A", "url": "https://a", "snippet": "sa"}]}
        return {"action_required": True, "action": "login", "tab_id": 1}

    async with drive_client:
        caller = mcpserver.search_and_fetch("q", k=1)
        _, out = await asyncio.gather(drive_extension(drive_client, respond, polls=400), caller)
    body = json.loads(out)
    assert body["results"][0]["fetch_error"].startswith("action_required")
```
Run: `cd server && uv run pytest tests/test_search_and_fetch.py -v` → PASS. Then full suite. Commit:
```bash
git add server/browser_relay/mcp_server/server.py server/tests/test_search_and_fetch.py
git commit -m "fix(mcp): label per-result escalation as action_required in search_and_fetch"
```

---

### Task 4: Extension — escalate on block, recheck held tab on resume

**Files:**
- Modify: `extension/engines/bing.js` (add `detectLogin` for fetch login walls? NO — keep scope: block detection only for search; login detection is generic, see note)
- Modify: `extension/background.js`
- Modify: `extension/build-inject.mjs` (no change expected; verify regen)

**Interfaces:**
- Consumes: `/pending` jobs may now carry `recheck_tab_id`; `detectBlock`.
- Produces: `background.js` `handleSearch`/`handleFetch` updated so:
  - On `detectBlock` true → instead of `{error}`, set the tab `active:true`, do NOT remove it, and POST `{action_required:true, action:"solve_captcha", tab_id}`.
  - When a job carries `recheck_tab_id` → re-use that tab (don't create a new one), re-inject + re-parse/extract; on success post the result and remove the tab; on still-blocked post `action_required` again (tab stays).
  - Fetch: detect a login wall via a generic heuristic and escalate with `action:"login"`; otherwise behavior unchanged.

- [ ] **Step 1: Add a generic login-wall detector to extract.js**

In `extension/extract.js`, add an exported helper (pure):
```javascript
// Heuristic: does this page look like it is gating content behind sign-in?
// Conservative — only fires on strong signals so normal articles aren't misflagged.
export function detectLoginWall(doc) {
  const url = (doc.location && doc.location.href) || "";
  if (/\/login|\/signin|\/sign-in|accounts\.google\.com|auth/i.test(url)) {
    // A password field present on a login-looking URL is a strong signal.
    if (doc.querySelector('input[type="password"]')) return true;
  }
  const bodyText = (doc.body?.textContent || "").toLowerCase();
  const hasPassword = !!doc.querySelector('input[type="password"]');
  if (hasPassword && /(sign in|log in|sign-in|log-in)/.test(bodyText) && bodyText.length < 4000) {
    return true; // small page dominated by a login form
  }
  return false;
}
```

- [ ] **Step 2: Update `engines/bing.js` injection — no change to parse, but expose detectBlock (already exposed)**

No code change to `bing.js` (it already exports `detectBlock`). Confirm the injected `__serp.detectBlock` is what `handleSearch` calls.

- [ ] **Step 3: Regenerate inject bundles (extract.js gained an export)**

Run:
```bash
cd /Users/bkumara/personal/localvily && npm run build:inject
grep -c "detectLoginWall" extension/inject/extract.js
```
Expected: `detectLoginWall` appears in the generated extract bundle (count >= 1). Update the `__extract` global if needed so the worker can call both extract and login detection — change `build-inject.mjs`'s extract footer to:
```javascript
`${extract}\nglobalThis.__extract = (doc) => extractContent(doc, window.Readability);\nglobalThis.__detectLogin = (doc) => detectLoginWall(doc);\n`,
```
Re-run `npm run build:inject` and `npm test` (the drift test will require the committed bundle to match).

- [ ] **Step 4: Update `background.js` search + fetch handlers**

Replace `handleSearch` and `handleFetch` in `extension/background.js`:
```javascript
async function openOrReuseTab(job, url) {
  if (job.recheck_tab_id != null) {
    try {
      const tab = await chrome.tabs.get(job.recheck_tab_id);
      return { tab, reused: true };
    } catch {
      // tab gone — fall through to a fresh one
    }
  }
  const tab = await chrome.tabs.create({ url, active: false });
  return { tab, reused: false };
}

async function handleSearch(job) {
  const { getEngine } = await import("./engines/index.js");
  const engine = getEngine(job.engine);
  const url = engine.serpUrl(job.query, job.k || 10);

  const { tab, reused } = await openOrReuseTab(job, url);
  let escalated = false;
  try {
    await waitForComplete(tab.id, TAB_LOAD_TIMEOUT);
    await new Promise((r) => setTimeout(r, FETCH_SETTLE_MS));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["inject/serp.js"] });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [job.k || 10],
      func: (k) => {
        if (globalThis.__serp.detectBlock(document)) return { blocked: true };
        return { results: globalThis.__serp.parse(document, k) };
      },
    });
    if (result.blocked) {
      escalated = true;
      await chrome.tabs.update(tab.id, { active: true }); // surface to the user
      await postResult(job.job_id, { action_required: true, action: "solve_captcha", tab_id: tab.id });
    } else {
      await postResult(job.job_id, { results: result.results });
    }
  } finally {
    if (!escalated) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function handleFetch(job) {
  const { tab, reused } = await openOrReuseTab(job, job.url);
  let escalated = false;
  try {
    await waitForComplete(tab.id, TAB_LOAD_TIMEOUT);
    await new Promise((r) => setTimeout(r, FETCH_SETTLE_MS));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["lib/Readability.js", "inject/extract.js"] });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({ login: globalThis.__detectLogin(document), content: globalThis.__extract(document) }),
    });
    if (result.login) {
      escalated = true;
      await chrome.tabs.update(tab.id, { active: true });
      await postResult(job.job_id, { action_required: true, action: "login", tab_id: tab.id });
    } else if (!result.content || !result.content.text) {
      await postResult(job.job_id, { error: "no extractable content" });
    } else {
      await postResult(job.job_id, result.content);
    }
  } finally {
    if (!escalated) chrome.tabs.remove(tab.id).catch(() => {});
  }
}
```

- [ ] **Step 5: Verify build + parse + unit suite**

Run:
```bash
cd /Users/bkumara/personal/localvily && npm run build:inject && npm test
node --input-type=module -e "import('node:fs').then(fs=>{new Function(fs.readFileSync('extension/background.js','utf8').replace(/chrome\./g,'globalThis.__c?.').replace(/await import\([^)]*\)/g,'({})')); console.log('parses OK')})" 2>&1 | tail -1
```
Expected: `npm test` green (incl. the inject-build drift test now covering `detectLoginWall`), background.js parses OK.

- [ ] **Step 6: Add a unit test for `detectLoginWall`**

Create `extension/tests/login.test.mjs`:
```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { detectLoginWall } from "../extract.js";

test("detects a login wall: password field on a signin URL", () => {
  const dom = new JSDOM(`<body><form>Please sign in <input type="password"></form></body>`,
    { url: "https://example.com/login" });
  assert.equal(detectLoginWall(dom.window.document), true);
});

test("does not misflag a normal article", () => {
  const dom = new JSDOM(`<body><article>${"word ".repeat(500)}</article></body>`,
    { url: "https://example.com/article" });
  assert.equal(detectLoginWall(dom.window.document), false);
});
```
Run: `cd /Users/bkumara/personal/localvily && node --test "extension/tests/login.test.mjs"` → PASS (2).

- [ ] **Step 7: Commit**

```bash
git add extension/extract.js extension/build-inject.mjs extension/inject/ extension/background.js extension/tests/login.test.mjs
git commit -m "feat(ext): escalate on block/login (keep+surface tab), recheck held tab on resume"
```

---

## Self-Review

**1. Spec coverage (Plan 3 / spec §10 escalation):**
- `action_required` status + payload (`action`, `url`, `message`, `resume_token`, `driver`) → Task 1. ✓
- Action registry, `/resume`, recheck job, TTL expiry, close_tabs draining → Task 2. ✓
- `resume` MCP tool, `health.pending_actions` → Task 3. ✓
- Extension: surface window + keep tab + post action_required; recheck held tab; login detection → Task 4. ✓
- Degradation (no human → TTL expiry → tab closed, no hang): Task 2 sweep + non-blocking original call. ✓
- `search_and_fetch` non-interactive per-result escalation (records fetch_error) — covered by existing Phase 1 behavior (a `/fetch` returning `action_required` → `_fetch_one` sees status != "ok" → sets `fetch_error`). ✓ NOTE: verify in Task 3 that `_fetch_one`'s `fetch_error` captures the action_required case; if it only checks `error`, adjust to use the action message. (Add to Task 3 if needed.)

**2. Placeholder scan:** all code blocks complete. The `_lifespan` task is fully specified. No TBD/"handle errors".

**3. Type consistency:** `Action` fields (`resume_token`, `kind`, `payload`, `tab_id`, `action`, `created_at`, `resolved`), `_register_action`, `_action_required_payload`, `_sweep_expired_actions`, `actions` registry, `action_close_queue`, `recheck_tab_id` payload key, `__detectLogin`/`__extract` injected globals — all consistent across tasks and between relay + extension.

---

## Note for Plan 4 (cloak)
- The cloak driver will reuse the SAME action_required/resume contract: on block/login it calls `page.bring_to_front()`, keeps the page, and the relay registers the action identically. `resume` re-checks the held page. The relay-side registry/`/resume`/TTL built here is driver-agnostic (it only holds a `tab_id`/page handle and a kind+payload), so cloak slots in by producing the same escalation signal.
