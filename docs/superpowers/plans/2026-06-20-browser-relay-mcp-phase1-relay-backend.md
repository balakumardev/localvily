# browser-relay-mcp — Plan 1: Relay Backend + MCP Server (Python) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the browser-independent half of browser-relay-mcp — a FastAPI relay (job queues, batch dispatch, adaptive-split concurrency, backpressure, health) plus the FastMCP server exposing `search`/`fetch`/`search_and_fetch`/`health`, all fully unit-tested against a simulated extension client.

**Architecture:** A FastAPI relay holds two in-memory job queues (search, fetch). MCP tools call blocking HTTP endpoints (`/search`, `/fetch`) that wait on an `asyncio.Event`; a browser extension (built in Plan 2) polls `/pending` for a *batch* of dispatchable jobs and posts results to `/result/{job_id}`. The MCP server auto-spawns/reuses a single shared relay (file-lock + version-kill + parent watchdog, ported from `~/personal/google-ai-scraper`). This plan simulates the extension with a pytest async helper, so the entire coordination layer is testable without a browser.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, httpx, `mcp` (FastMCP), pytest + pytest-asyncio. Packaged for `uvx`/PyPI as `browser-relay-mcp`.

## Global Constraints

- **Python floor:** 3.13 (matches google-ai-scraper).
- **Relay port:** `15552` (default; distinct from google-ai-scraper's `15551`). Env override `BROWSER_RELAY_URL`.
- **MCP server id:** `browser-relay`. Console entry point: `browser-relay-mcp` → `browser_relay.mcp_server.server:main`.
- **Tool result envelope:** every tool returns a JSON string with top-level `status` ∈ `{"ok","action_required","error"}`. Plan 1 implements `ok` and `error` only (`action_required` arrives in Plan 3).
- **Driver param:** all search/fetch tools take `driver: str = "relay"`. Plan 1 implements `relay` only; `driver="cloak"` returns `{"status":"error","error":"cloak driver not available in this build"}`.
- **Concurrency defaults (configurable via env):** `FETCH_CAP=5`, `SEARCH_CONCURRENCY=1`, `SEARCH_MIN_SPACING_MS=500`, `QUERY_TIMEOUT=110.0`, `FETCH_TIMEOUT=60.0`, `EXTENSION_RECENT_POLL_THRESHOLD=75.0`.
- **Fail loud, never silent-empty:** down/timeout → explicit `status:"error"`; a real zero-result search → `status:"ok", count:0` (legitimate, flagged). Never return empty masquerading as success.
- **Git:** repo init is pending user confirmation. If `~/personal/localvily` is not yet a git repo when execution starts, run `git init` (personal account → `GH_HOST=github.com`) in Task 1; otherwise skip the init and keep the per-task commits.

---

### Task 1: Project scaffolding + `/version` and `/health` stub

**Files:**
- Create: `server/pyproject.toml`
- Create: `server/browser_relay/__init__.py`
- Create: `server/browser_relay/app.py`
- Create: `server/tests/__init__.py`
- Create: `server/tests/test_health.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `browser_relay.__version__: str`; FastAPI `app` in `browser_relay.app` with `GET /version` → `{"version": str}` and `GET /health` → `{"status":"ok", ...}`.

- [ ] **Step 1: Create the package config**

`server/pyproject.toml`:
```toml
[project]
name = "browser-relay-mcp"
version = "0.1.0"
description = "Unlimited, session-authenticated web search & fetch via the user's browser, behind an MCP server"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "httpx>=0.27",
    "mcp>=1.2",
]

[project.scripts]
browser-relay-mcp = "browser_relay.mcp_server.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["browser_relay"]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create the version module**

`server/browser_relay/__init__.py`:
```python
__version__ = "0.1.0"
```

- [ ] **Step 3: Write the failing test**

`server/tests/__init__.py`: (empty file)

`server/tests/test_health.py`:
```python
import httpx
from httpx import ASGITransport

from browser_relay import __version__
from browser_relay.app import app


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_version_endpoint():
    async with await _client() as client:
        resp = await client.get("/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": __version__}


async def test_health_reports_ok_and_disconnected_before_any_poll():
    async with await _client() as client:
        resp = await client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["extension_connected"] is False
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'browser_relay.app'` (or ImportError for `app`).

- [ ] **Step 5: Write minimal implementation**

`server/browser_relay/app.py`:
```python
import time

from fastapi import FastAPI

from browser_relay import __version__

EXTENSION_RECENT_POLL_THRESHOLD = 75.0  # seconds; MV3 workers sleep between alarm wakeups

last_poll_time: float = 0.0

app = FastAPI()


def _extension_connected() -> bool:
    if last_poll_time == 0:
        return False
    return (time.monotonic() - last_poll_time) <= EXTENSION_RECENT_POLL_THRESHOLD


@app.get("/version")
async def version():
    return {"version": __version__}


@app.get("/health")
async def health():
    return {"status": "ok", "extension_connected": _extension_connected()}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd server && uv run pytest tests/test_health.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
# If not already a git repo (see Global Constraints): GH_HOST=github.com git init
git add server/pyproject.toml server/browser_relay/__init__.py server/browser_relay/app.py server/tests/
git commit -m "feat(relay): scaffold package with /version and /health stub"
```

---

### Task 2: Job model + `/search`, `/fetch`, `/pending`, `/result` round-trip

**Files:**
- Modify: `server/browser_relay/app.py`
- Create: `server/tests/conftest.py`
- Create: `server/tests/test_roundtrip.py`

**Interfaces:**
- Consumes: `app` from Task 1.
- Produces:
  - `GET /search?q=&k=&engine=&driver=` → blocks, returns `{"status":"ok","query","engine","driver":"relay","count","results":[...]}` or `{"status":"error","error",...}`.
  - `GET /fetch?url=&include_html=&driver=` → blocks, returns `{"status":"ok","url","driver":"relay","title","text","excerpt","length","html"?}` or error.
  - `GET /pending` → `{"jobs":[{"job_id","kind",...payload}], "close_tabs":[]}`; updates `last_poll_time`.
  - `POST /result/{job_id}` with arbitrary JSON body → sets the job's result; `{"status":"ok"}`.
  - Test helper `drive_extension(client, results_for)` in `conftest.py`.

- [ ] **Step 1: Write the failing test for a search round-trip**

`server/tests/conftest.py`:
```python
import asyncio

import httpx
import pytest
from httpx import ASGITransport

from browser_relay.app import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def drive_extension(client, respond, *, polls=200, delay=0.01):
    """Simulate the browser extension: poll /pending, post a result for each job.

    `respond(job)` returns the JSON body to POST to /result/{job_id}.
    Stops after it has answered at least one job and the next poll is empty.
    """
    answered = 0
    for _ in range(polls):
        r = await client.get("/pending")
        jobs = r.json()["jobs"]
        if not jobs:
            if answered:
                return
            await asyncio.sleep(delay)
            continue
        for job in jobs:
            await client.post(f"/result/{job['job_id']}", json=respond(job))
            answered += 1
```

`server/tests/test_roundtrip.py`:
```python
import asyncio

from tests.conftest import drive_extension


async def test_search_roundtrip(client):
    def respond(job):
        assert job["kind"] == "search"
        assert job["query"] == "consistent hashing"
        return {"results": [{"title": "T", "url": "https://e.com", "snippet": "s"}]}

    caller = client.get("/search", params={"q": "consistent hashing", "k": 10})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] == "relay"
    assert body["count"] == 1
    assert body["results"][0]["url"] == "https://e.com"


async def test_fetch_roundtrip(client):
    def respond(job):
        assert job["kind"] == "fetch"
        return {"title": "Doc", "text": "x" * 1500, "excerpt": "ex", "length": 1500}

    caller = client.get("/fetch", params={"url": "https://e.com/a"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "ok"
    assert body["length"] == 1500
    assert body["title"] == "Doc"


async def test_search_error_payload_marks_status_error(client):
    def respond(job):
        return {"error": "blocked"}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_roundtrip.py -v`
Expected: FAIL — `/search` returns 404 (route not defined).

- [ ] **Step 3: Implement the job model and endpoints**

Replace the entire contents of `server/browser_relay/app.py` with:
```python
import asyncio
import time
import uuid
from collections import deque

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from browser_relay import __version__

EXTENSION_RECENT_POLL_THRESHOLD = 75.0  # seconds
QUERY_TIMEOUT = 110.0
FETCH_TIMEOUT = 60.0


class Job:
    __slots__ = ("job_id", "kind", "payload", "event", "result", "dispatched")

    def __init__(self, kind: str, payload: dict):
        self.job_id = uuid.uuid4().hex[:12]
        self.kind = kind  # "search" | "fetch"
        self.payload = payload
        self.event = asyncio.Event()
        self.result: dict | None = None
        self.dispatched = False


jobs: dict[str, Job] = {}
search_queue: deque[Job] = deque()
fetch_queue: deque[Job] = deque()
last_poll_time: float = 0.0

app = FastAPI()


class ResultBody(BaseModel):
    model_config = {"extra": "allow"}


def _extension_connected() -> bool:
    if last_poll_time == 0:
        return False
    return (time.monotonic() - last_poll_time) <= EXTENSION_RECENT_POLL_THRESHOLD


def _shape_search(job: Job) -> dict:
    base = {
        "query": job.payload["query"],
        "engine": job.payload["engine"],
        "driver": "relay",
    }
    result = job.result or {"error": "no result"}
    if "error" in result:
        return {"status": "error", **base, "error": result["error"]}
    results = result.get("results", [])
    return {"status": "ok", **base, "count": len(results), "results": results}


def _shape_fetch(job: Job) -> dict:
    base = {"url": job.payload["url"], "driver": "relay"}
    result = job.result or {"error": "no result"}
    if "error" in result:
        return {"status": "error", **base, "error": result["error"]}
    out = {"status": "ok", **base}
    for key in ("title", "text", "excerpt", "length", "html"):
        if key in result:
            out[key] = result[key]
    return out


async def _await_job(job: Job, queue: deque, timeout: float):
    queue.append(job)
    jobs[job.job_id] = job
    try:
        await asyncio.wait_for(job.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, "extension did not respond in time")
    finally:
        jobs.pop(job.job_id, None)
        try:
            queue.remove(job)
        except ValueError:
            pass


@app.get("/version")
async def version():
    return {"version": __version__}


@app.get("/health")
async def health():
    return {"status": "ok", "extension_connected": _extension_connected()}


@app.get("/search")
async def search(q: str, k: int = 10, engine: str = "bing", driver: str = "relay"):
    if driver != "relay":
        return {"status": "error", "error": "cloak driver not available in this build"}
    if not q.strip():
        raise HTTPException(400, "query is required")
    job = Job("search", {"query": q.strip(), "k": k, "engine": engine})
    await _await_job(job, search_queue, QUERY_TIMEOUT)
    return _shape_search(job)


@app.get("/fetch")
async def fetch(url: str, include_html: bool = False, driver: str = "relay"):
    if driver != "relay":
        return {"status": "error", "error": "cloak driver not available in this build"}
    if not url.strip():
        raise HTTPException(400, "url is required")
    job = Job("fetch", {"url": url.strip(), "include_html": include_html})
    await _await_job(job, fetch_queue, FETCH_TIMEOUT)
    return _shape_fetch(job)


@app.get("/pending")
async def pending():
    global last_poll_time
    last_poll_time = time.monotonic()
    batch = []
    for queue in (search_queue, fetch_queue):
        for job in list(queue):
            job.dispatched = True
            batch.append({"job_id": job.job_id, "kind": job.kind, **job.payload})
    return {"jobs": batch, "close_tabs": []}


@app.post("/result/{job_id}")
async def post_result(job_id: str, body: ResultBody):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or expired")
    job.result = body.model_dump()
    job.event.set()
    return {"status": "ok"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_roundtrip.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/browser_relay/app.py server/tests/conftest.py server/tests/test_roundtrip.py
git commit -m "feat(relay): job model with /search /fetch /pending /result round-trip"
```

---

### Task 3: Batch dispatch with adaptive-split caps, spacing, backpressure, timeout accounting

**Files:**
- Modify: `server/browser_relay/app.py`
- Create: `server/tests/test_dispatch.py`

**Interfaces:**
- Consumes: job model + endpoints from Task 2.
- Produces: `/pending` now respects `FETCH_CAP` (≤5 fetches in flight), `SEARCH_CONCURRENCY` (≤1 search in flight) and `SEARCH_MIN_SPACING_MS` (≥500ms between dispatched searches); in-flight counters `search_in_flight`/`fetch_in_flight` decremented on `/result` and on timeout. Env overrides read at import.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_dispatch.py`:
```python
import asyncio

import browser_relay.app as appmod


def setup_function():
    # reset module state between tests
    appmod.search_queue.clear()
    appmod.fetch_queue.clear()
    appmod.jobs.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0
    appmod.last_poll_time = 0.0


async def _enqueue(client, kind, n):
    """Fire n blocking calls without awaiting them; return the asyncio tasks."""
    tasks = []
    for i in range(n):
        if kind == "fetch":
            tasks.append(asyncio.create_task(client.get("/fetch", params={"url": f"https://e/{i}"})))
        else:
            tasks.append(asyncio.create_task(client.get("/search", params={"q": f"q{i}"})))
    await asyncio.sleep(0.05)  # let them enqueue
    return tasks


async def test_fetch_cap_limits_in_flight(client):
    tasks = await _enqueue(client, "fetch", 8)
    r = await client.get("/pending")
    assert len(r.json()["jobs"]) == appmod.FETCH_CAP  # only 5 dispatched at once
    for t in tasks:
        t.cancel()


async def test_search_spacing_blocks_second_immediate_dispatch(client):
    tasks = await _enqueue(client, "search", 2)
    first = await client.get("/pending")
    assert len(first["jobs"] if isinstance(first, dict) else first.json()["jobs"]) == 1
    second = await client.get("/pending")  # immediately after — within spacing window
    assert len(second.json()["jobs"]) == 0
    for t in tasks:
        t.cancel()


async def test_result_frees_fetch_capacity(client):
    tasks = await _enqueue(client, "fetch", 6)
    r1 = await client.get("/pending")
    dispatched = r1.json()["jobs"]
    assert len(dispatched) == appmod.FETCH_CAP
    await client.post(f"/result/{dispatched[0]['job_id']}", json={"title": "t", "text": "x", "length": 1})
    await asyncio.sleep(0.02)
    r2 = await client.get("/pending")
    assert len(r2.json()["jobs"]) == 1  # the 6th job now fits
    for t in tasks:
        t.cancel()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_dispatch.py -v`
Expected: FAIL — `/pending` dispatches all 8 fetches (no cap), and `AttributeError: module ... has no attribute 'search_in_flight'`.

- [ ] **Step 3: Add config + counters near the top of `app.py`**

In `server/browser_relay/app.py`, add these imports/constants after the existing constants block (below `FETCH_TIMEOUT = 60.0`):
```python
import os

FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
SEARCH_CONCURRENCY = int(os.environ.get("BROWSER_RELAY_SEARCH_CONCURRENCY", "1"))
SEARCH_MIN_SPACING_MS = int(os.environ.get("BROWSER_RELAY_SEARCH_MIN_SPACING_MS", "500"))

search_in_flight: int = 0
fetch_in_flight: int = 0
last_search_dispatch: float = 0.0
```

- [ ] **Step 4: Replace `/pending` to enforce caps + spacing**

Replace the `pending()` function in `app.py` with:
```python
@app.get("/pending")
async def pending():
    global last_poll_time, search_in_flight, fetch_in_flight, last_search_dispatch
    last_poll_time = time.monotonic()
    now = time.monotonic()
    batch = []

    # Searches: near-serial with spacing.
    while (
        search_queue
        and search_in_flight < SEARCH_CONCURRENCY
        and (now - last_search_dispatch) * 1000 >= SEARCH_MIN_SPACING_MS
    ):
        job = search_queue[0]
        if job.dispatched:
            break
        job.dispatched = True
        search_in_flight += 1
        last_search_dispatch = now
        batch.append({"job_id": job.job_id, "kind": job.kind, **job.payload})

    # Fetches: parallel up to FETCH_CAP.
    for job in fetch_queue:
        if fetch_in_flight >= FETCH_CAP:
            break
        if job.dispatched:
            continue
        job.dispatched = True
        fetch_in_flight += 1
        batch.append({"job_id": job.job_id, "kind": job.kind, **job.payload})

    return {"jobs": batch, "close_tabs": []}
```

- [ ] **Step 5: Decrement counters on result and on timeout**

Replace `post_result` with:
```python
def _release(job: Job):
    global search_in_flight, fetch_in_flight
    if not job.dispatched:
        return
    job.dispatched = False
    if job.kind == "search":
        search_in_flight = max(0, search_in_flight - 1)
    else:
        fetch_in_flight = max(0, fetch_in_flight - 1)


@app.post("/result/{job_id}")
async def post_result(job_id: str, body: ResultBody):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or expired")
    job.result = body.model_dump()
    _release(job)
    job.event.set()
    return {"status": "ok"}
```

And in `_await_job`, release on timeout — replace its `finally` block with:
```python
    finally:
        jobs.pop(job.job_id, None)
        try:
            queue.remove(job)
        except ValueError:
            pass
        _release(job)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_dispatch.py tests/test_roundtrip.py -v`
Expected: PASS (all). The round-trip tests still pass because `/result` releases capacity.

- [ ] **Step 7: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_dispatch.py
git commit -m "feat(relay): batch dispatch with fetch cap, search spacing, in-flight accounting"
```

---

### Task 4: Full `/health` shape (queue depths, in-flight, version, engine)

**Files:**
- Modify: `server/browser_relay/app.py`
- Modify: `server/tests/test_health.py`

**Interfaces:**
- Consumes: counters + queues from Task 3.
- Produces: `/health` →
  `{"status":"ok","extension_connected":bool,"extension_status":str,"last_poll_age_seconds":float|None,"search_queued":int,"fetch_queued":int,"in_flight":int,"max_fetch_tabs":int,"engine":str,"version":str}`.

- [ ] **Step 1: Extend the failing test**

Append to `server/tests/test_health.py`:
```python
import asyncio

import browser_relay.app as appmod
from tests.conftest import drive_extension  # noqa: F401  (import style parity)


async def test_health_reports_queue_depths_and_caps(client):
    appmod.search_queue.clear()
    appmod.fetch_queue.clear()
    appmod.jobs.clear()
    appmod.fetch_in_flight = 0
    appmod.search_in_flight = 0

    t = asyncio.create_task(client.get("/fetch", params={"url": "https://e/x"}))
    await asyncio.sleep(0.05)
    body = (await client.get("/health")).json()
    assert body["fetch_queued"] == 1
    assert body["max_fetch_tabs"] == appmod.FETCH_CAP
    assert body["engine"] == "bing"
    assert "version" in body
    t.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_health.py::test_health_reports_queue_depths_and_caps -v`
Expected: FAIL — `KeyError: 'fetch_queued'`.

- [ ] **Step 3: Replace `health()` with the full shape**

Replace the `health()` function in `app.py` with:
```python
DEFAULT_ENGINE = os.environ.get("BROWSER_RELAY_DEFAULT_ENGINE", "bing")


@app.get("/health")
async def health():
    poll_age = None if last_poll_time == 0 else round(time.monotonic() - last_poll_time, 1)
    connected = _extension_connected()
    return {
        "status": "ok",
        "extension_connected": connected,
        "extension_status": "connected" if connected else ("stale" if last_poll_time else "never_seen"),
        "last_poll_age_seconds": poll_age,
        "search_queued": len(search_queue),
        "fetch_queued": len(fetch_queue),
        "in_flight": search_in_flight + fetch_in_flight,
        "max_fetch_tabs": FETCH_CAP,
        "engine": DEFAULT_ENGINE,
        "version": __version__,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_health.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_health.py
git commit -m "feat(relay): full /health shape with queue depths, caps, version"
```

---

### Task 5: MCP server — tools (`search`, `fetch`, `health`) + backend lifecycle

**Files:**
- Create: `server/browser_relay/mcp_server/__init__.py`
- Create: `server/browser_relay/mcp_server/server.py`
- Create: `server/tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: the running relay (`app`) over HTTP.
- Produces:
  - Overridable client factory `_client() -> httpx.AsyncClient` (tests monkeypatch it to an ASGI transport).
  - `async search(query, k=10, engine="bing", driver="relay") -> str` (JSON).
  - `async fetch(url, include_html=False, driver="relay") -> str` (JSON).
  - `async health() -> str` (JSON).
  - `main()` console entry that auto-spawns/reuses the shared backend, then runs the MCP server (stdio default, `--sse` option, `--backend` to run only the relay).

- [ ] **Step 1: Write the failing test (tools pass through to the relay)**

`server/tests/test_mcp_tools.py`:
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
    appmod.search_queue.clear()
    appmod.fetch_queue.clear()
    appmod.jobs.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


def _patch_client(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_search_tool_passthrough(monkeypatch):
    drive_client = _patch_client(monkeypatch)

    def respond(job):
        return {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}

    async with drive_client:
        caller = mcpserver.search("consistent hashing", k=10)
        _, out = await asyncio.gather(drive_extension(drive_client, respond), caller)
    body = json.loads(out)
    assert body["status"] == "ok"
    assert body["count"] == 1


async def test_cloak_driver_rejected(monkeypatch):
    _patch_client(monkeypatch)
    out = await mcpserver.search("x", driver="cloak")
    body = json.loads(out)
    assert body["status"] == "error"
    assert "cloak" in body["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_mcp_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'browser_relay.mcp_server'`.

- [ ] **Step 3: Implement the MCP server**

`server/browser_relay/mcp_server/__init__.py`: (empty file)

`server/browser_relay/mcp_server/server.py` — backend lifecycle ported verbatim from `~/personal/google-ai-scraper/server/google_ai_scraper/mcp_server/server.py` (renamed: `GOOGLE_AI_SCRAPER_URL`→`BROWSER_RELAY_URL`, port `15551`→`15552`, state dir `google-ai-scraper`→`browser-relay`, module `google_ai_scraper.mcp_server.server`→`browser_relay.mcp_server.server`, app import `google_ai_scraper.app`→`browser_relay.app`), with the tool surface below:
```python
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from browser_relay import __version__

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 15552
SERVER_URL = os.environ.get("BROWSER_RELAY_URL", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
REQUEST_TIMEOUT = 120.0
BACKEND_STARTUP_TIMEOUT = 10.0
HEALTHCHECK_TIMEOUT = 1.5

AUTO_MANAGE_SERVER = False
MANAGED_BACKEND_PORT: int | None = None

mcp = FastMCP("browser-relay", port=8002)


def _client() -> httpx.AsyncClient:
    """Overridable factory (tests patch this)."""
    return httpx.AsyncClient(base_url=SERVER_URL, timeout=REQUEST_TIMEOUT)


def _state_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = base / "browser-relay"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- backend lifecycle: PORT VERBATIM from google-ai-scraper ---
# _backend_lock_path, _backend_log_path, _BackendLock, _manageable_local_port,
# _backend_healthy, _wait_for_backend, _spawn_backend_process, _ensure_local_backend,
# _backend_pid_path, _backend_version, _kill_stale_backend, _kill_backend_by_port,
# _ensure_backend_current, _run_backend, _parent_watchdog
# Copy these functions unchanged except: module string in _spawn_backend_process args is
# "browser_relay.mcp_server.server", and _run_backend imports `from browser_relay.app import app`.


async def _request(method: str, path: str, **kwargs) -> dict:
    """Call the relay, returning a dict (never raises)."""
    try:
        async with _client() as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.ConnectError:
        if AUTO_MANAGE_SERVER and MANAGED_BACKEND_PORT is not None:
            try:
                _ensure_local_backend(SERVER_URL, MANAGED_BACKEND_PORT)
                async with _client() as client:
                    resp = await client.request(method, path, **kwargs)
            except Exception:
                return {"status": "error", "error": f"Cannot connect to relay at {SERVER_URL}"}
        else:
            return {"status": "error", "error": f"Cannot connect to relay at {SERVER_URL}"}
    except httpx.ReadTimeout:
        return {"status": "error", "error": "Timed out waiting for the browser relay"}

    if resp.status_code == 200:
        return resp.json()
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return {"status": "error", "error": f"Relay error ({resp.status_code}): {detail}"}


@mcp.tool()
async def search(query: str, k: int = 10, engine: str = "bing", driver: str = "relay") -> str:
    """Web search via the user's browser. Returns {status, results:[{title,url,snippet}]}.

    driver: "relay" (default, the user's logged-in Chrome) or "cloak" (stealth headless; not in this build).
    """
    result = await _request("GET", "/search", params={"q": query, "k": k, "engine": engine, "driver": driver})
    return json.dumps(result, indent=2)


@mcp.tool()
async def fetch(url: str, include_html: bool = False, driver: str = "relay") -> str:
    """Load a URL in the browser and return its clean readable main content as {status, title, text, ...}."""
    result = await _request(
        "GET", "/fetch",
        params={"url": url, "include_html": str(include_html).lower(), "driver": driver},
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def health() -> str:
    """Check relay + browser-extension connectivity and queue depth."""
    return json.dumps(await _request("GET", "/health"), indent=2)


def main():
    parser = argparse.ArgumentParser(description="browser-relay MCP server")
    parser.add_argument("--sse", action="store_true")
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--backend", action="store_true")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    global AUTO_MANAGE_SERVER, MANAGED_BACKEND_PORT, SERVER_URL
    if args.backend:
        _run_backend(args.port)
        return

    SERVER_URL = os.environ.get("BROWSER_RELAY_URL", f"http://{DEFAULT_HOST}:{args.port}")
    MANAGED_BACKEND_PORT = _manageable_local_port(SERVER_URL)
    AUTO_MANAGE_SERVER = not args.no_server and MANAGED_BACKEND_PORT is not None
    if AUTO_MANAGE_SERVER:
        _ensure_backend_current(SERVER_URL, MANAGED_BACKEND_PORT)
        _ensure_local_backend(SERVER_URL, MANAGED_BACKEND_PORT)

    if args.sse:
        mcp.run(transport="sse")
    else:
        threading.Thread(target=_parent_watchdog, daemon=True).start()
        mcp.run()


if __name__ == "__main__":
    main()
```

> Implementer note: open `~/personal/google-ai-scraper/server/google_ai_scraper/mcp_server/server.py` and copy the lifecycle helpers listed in the comment block verbatim, applying only the rename rules stated. Do not re-derive them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Manual smoke test of backend auto-spawn**

Run: `cd server && uv run browser-relay-mcp --backend --port 15552 &` then `curl -s localhost:15552/health | python3 -m json.tool`
Expected: JSON with `"status": "ok"`, `"extension_connected": false`. Then `kill %1`.

- [ ] **Step 6: Commit**

```bash
git add server/browser_relay/mcp_server/ server/tests/test_mcp_tools.py
git commit -m "feat(mcp): search/fetch/health tools + backend spawn/lock/watchdog"
```

---

### Task 6: `search_and_fetch` tool (search → parallel fetch of top-k)

**Files:**
- Modify: `server/browser_relay/mcp_server/server.py`
- Create: `server/tests/test_search_and_fetch.py`

**Interfaces:**
- Consumes: `_request`, relay `/search` + `/fetch`.
- Produces: `async search_and_fetch(query, k=5, engine="bing", driver="relay") -> str` →
  `{"status":"ok","query","engine","driver","count","results":[{title,url,snippet,text,length,fetch_error}]}`. A failed per-result fetch sets `fetch_error` (string) with `text=""`; the batch still returns. If the search step itself errors, returns that error envelope.

- [ ] **Step 1: Write the failing test**

`server/tests/test_search_and_fetch.py`:
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
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


async def test_search_and_fetch_combines_results(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    drive_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    def respond(job):
        if job["kind"] == "search":
            return {"results": [
                {"title": "A", "url": "https://a", "snippet": "sa"},
                {"title": "B", "url": "https://b", "snippet": "sb"},
            ]}
        # fetch: fail for b, succeed for a
        if job["url"] == "https://b":
            return {"error": "nav failed"}
        return {"title": "A", "text": "y" * 1200, "excerpt": "e", "length": 1200}

    async with drive_client:
        caller = mcpserver.search_and_fetch("q", k=2)
        _, out = await asyncio.gather(drive_extension(drive_client, respond, polls=400), caller)
    body = json.loads(out)
    assert body["status"] == "ok"
    assert body["count"] == 2
    by_url = {r["url"]: r for r in body["results"]}
    assert by_url["https://a"]["length"] == 1200
    assert by_url["https://a"]["fetch_error"] is None
    assert by_url["https://b"]["fetch_error"] == "nav failed"
    assert by_url["https://b"]["text"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_search_and_fetch.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'search_and_fetch'`.

- [ ] **Step 3: Implement the tool**

Add to `server/browser_relay/mcp_server/server.py` (after the `fetch` tool):
```python
@mcp.tool()
async def search_and_fetch(query: str, k: int = 5, engine: str = "bing", driver: str = "relay") -> str:
    """Search, then fetch the full readable text of the top-k results in parallel.

    Returns {status, results:[{title,url,snippet,text,length,fetch_error}]}. A page that
    fails to fetch sets its own fetch_error and text=""; the batch still returns.
    """
    s = await _request("GET", "/search", params={"q": query, "k": k, "engine": engine, "driver": driver})
    if s.get("status") != "ok":
        return json.dumps(s, indent=2)

    results = s.get("results", [])[:k]

    async def _fetch_one(item: dict) -> dict:
        f = await _request("GET", "/fetch", params={"url": item["url"], "driver": driver})
        if f.get("status") == "ok":
            return {**item, "text": f.get("text", ""), "length": f.get("length", 0), "fetch_error": None}
        return {**item, "text": "", "length": 0, "fetch_error": f.get("error", "fetch failed")}

    merged = await asyncio.gather(*[_fetch_one(r) for r in results])
    return json.dumps(
        {"status": "ok", "query": query, "engine": engine, "driver": driver,
         "count": len(merged), "results": list(merged)},
        indent=2,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && uv run pytest tests/test_search_and_fetch.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add server/browser_relay/mcp_server/server.py server/tests/test_search_and_fetch.py
git commit -m "feat(mcp): search_and_fetch with parallel per-result fetch and fetch_error"
```

---

### Task 7: STORM adapter (`to_storm` + `chunk`)

**Files:**
- Create: `adapters/__init__.py`
- Create: `adapters/storm.py`
- Create: `adapters/tests/__init__.py`
- Create: `adapters/tests/test_storm.py`

**Interfaces:**
- Consumes: a parsed `search_and_fetch` output dict.
- Produces: `to_storm(result: dict) -> list[dict]` returning `[{url,title,description,snippets[]}]`, and `chunk(text: str, size: int = 1000) -> list[str]`.

- [ ] **Step 1: Write the failing test**

`adapters/__init__.py`: (empty)
`adapters/tests/__init__.py`: (empty)

`adapters/tests/test_storm.py`:
```python
from adapters.storm import chunk, to_storm


def test_chunk_splits_by_size():
    out = chunk("a" * 2500, size=1000)
    assert len(out) == 3
    assert "".join(out) == "a" * 2500


def test_to_storm_maps_shape_and_skips_empty_text():
    result = {
        "status": "ok",
        "results": [
            {"url": "https://a", "title": "A", "snippet": "sa", "text": "x" * 1500, "fetch_error": None},
            {"url": "https://b", "title": "B", "snippet": "sb", "text": "", "fetch_error": "nav failed"},
        ],
    }
    out = to_storm(result)
    assert len(out) == 1
    assert out[0]["url"] == "https://a"
    assert out[0]["description"] == "sa"
    assert "".join(out[0]["snippets"]) == "x" * 1500


def test_to_storm_returns_empty_on_non_ok():
    assert to_storm({"status": "error", "error": "down"}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/bkumara/personal/localvily && uv run --with pytest pytest adapters/tests/test_storm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'adapters.storm'`.

- [ ] **Step 3: Implement the adapter**

`adapters/storm.py`:
```python
def chunk(text: str, size: int = 1000) -> list[str]:
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def to_storm(result: dict) -> list[dict]:
    """Map a search_and_fetch() output dict to the STORM/dspy retriever shape."""
    if result.get("status") != "ok":
        return []
    return [
        {
            "url": x["url"],
            "title": x.get("title", ""),
            "description": x.get("snippet", ""),
            "snippets": chunk(x.get("text", "")),
        }
        for x in result.get("results", [])
        if x.get("text")
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/bkumara/personal/localvily && uv run --with pytest pytest adapters/tests/test_storm.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full suite**

Run: `cd server && uv run pytest -v`
Expected: PASS (all tasks' tests green).

- [ ] **Step 6: Commit**

```bash
git add adapters/
git commit -m "feat(adapter): STORM retriever adapter (to_storm + chunk)"
```

---

## Self-Review

**1. Spec coverage (Plan 1 portion of the spec):**
- §4 relay endpoints (`/search`,`/fetch`,`/pending`,`/result`,`/health`,`/version`) → Tasks 1–4. ✓
- §5 tool contract (`search`,`fetch`,`search_and_fetch`,`health`, `driver` param, `status` envelope) → Tasks 5–6. ✓ (`resume`/`action_required` deferred to Plan 3 per Global Constraints.)
- §6 batch dispatch, caps, spacing, backpressure, timeout accounting → Task 3. ✓
- §6 backend lifecycle (spawn/lock/version/watchdog) → Task 5. ✓
- §13 STORM adapter → Task 7. ✓
- Out of Plan 1 (correctly): extension/shared-JS (Plan 2), escalation (Plan 3), cloak (Plan 4), packaging assets (Plan 5).

**2. Placeholder scan:** Task 5 references porting named functions from google-ai-scraper rather than reprinting ~250 lines of known-good lifecycle code — this is an instruction to copy an existing real file verbatim with explicit rename rules, not an undefined forward-reference. All novel logic (relay, dispatch, tools, adapter) has complete code. No TBD/"handle errors"/"write tests for the above".

**3. Type consistency:** `Job(kind, payload)`, `_release(job)`, `_shape_search`/`_shape_fetch`, `_await_job(job, queue, timeout)`, `_client()`, `_request(method, path, **kwargs)`, `search_and_fetch(query, k, engine, driver)`, `to_storm(result)`/`chunk(text, size)` are used consistently across tasks and tests. Result envelope `status`/`results`/`count`/`fetch_error` consistent between `_shape_*` (Task 2/3), `search_and_fetch` (Task 6), and `to_storm` (Task 7).

---

## Notes for subsequent plans
- **Plan 2 (extension):** implements `engines/bing.js` (`serpUrl`/`detectBlock`/`parse`), `extract.js` (Readability + innerText fallback), `background.js` (poll `/pending`, drive tabs, POST `/result`), popup/options. Validated by jsdom unit tests + live acceptance #1–#5. The relay built here is its counterpart — run `browser-relay-mcp --backend` and load the unpacked extension against `localhost:15552`.
- **Plan 3 (escalation):** adds the action registry + `/resume/{token}`, `resume` tool, and `action_required` status; extends `_shape_*` to pass through an `action_required` result payload.
- **Plan 4 (cloak):** adds `browser_relay/drivers/cloak.py` (Playwright stealth persistent context) and routes `driver="cloak"` to it.
