# browser-relay-mcp — Plan 4: Cloak Driver (Embedded Stealth Browser) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `driver="cloak"`: an embedded, in-process stealth browser (patchright — a stealth-patched Playwright drop-in) with a persistent profile, that performs search/fetch on bot-protected or unattended pages, reusing the SAME shared SERP-parse + Readability extraction logic as the relay driver, and the SAME `action_required`/`resume` escalation contract.

**Architecture:** A `CloakDriver` class wraps a patchright `persistent context` (headed-but-backgrounded, profile under `.cloak-profile/`). The relay's `/search` and `/fetch`, when `driver="cloak"`, call the driver in-process (async) instead of enqueuing a poll job: the driver navigates a background page, injects the shared `inject/serp.js` / `lib/Readability.js`+`inject/extract.js` files via `page.add_script_tag`/`evaluate`, and returns the same result shapes. Escalation works by `page.bring_to_front()` + registering a cloak action whose "tab handle" is the page; `resume` re-drives that held page. This is **fully self-contained** — NO dependency on the external CloakBrowser MCP; patchright + Chromium run inside our own process.

**Tech Stack:** patchright (`pip install patchright`, `patchright install chromium`), async Playwright API. Reuses `extension/inject/serp.js` + `extension/inject/extract.js` + `extension/lib/Readability.js` (read from disk, evaluated in-page). Python stdlib + pytest; cloak tests that need a browser are guarded/skipped when Chromium isn't installed.

## Global Constraints

- **NO external CloakBrowser MCP.** patchright runs in-process inside our relay. Local-only; nothing third-party in the request path.
- **Engine:** `patchright` (stealth-patched Playwright drop-in). `from patchright.async_api import async_playwright`. Launch a **persistent context** with `user_data_dir = CLOAK_PROFILE_DIR` (env `BROWSER_RELAY_CLOAK_PROFILE_DIR`, default `<repo>/.cloak-profile`), channel `chrome` when available else bundled chromium, **headed** (`headless=False`) but windows created backgrounded; brought to front only on escalation.
- **Result shapes identical to relay driver:** search → `{status:"ok", query, engine, driver:"cloak", count, results:[{title,url,snippet}]}`; fetch → `{status:"ok", url, driver:"cloak", title, text, excerpt, length, html?}`; block/login → `{status:"action_required", action, url|query, message, resume_token, driver:"cloak"}`; failure → `{status:"error", driver:"cloak", error}`.
- **Shared DOM logic, single source of truth:** the cloak driver MUST evaluate the SAME `extension/inject/serp.js` (`globalThis.__serp`) and `extension/lib/Readability.js` + `extension/inject/extract.js` (`globalThis.__extract`, `globalThis.__detectLogin`) that the extension injects. No reimplementation of parsing/extraction in Python.
- **Generalize the 4 Phase-3 driver seams (from the Phase 3 whole-branch review):**
  1. `Action` gains a `driver` field (set at registration).
  2. `resume()` forks on `record.driver`: `relay` → enqueue recheck job (existing path); `cloak` → re-drive the held cloak page in-process.
  3. Replace the 3 hardcoded `driver:"relay"` strings (`_action_required_payload`, `health.pending_actions`, `_shape_search`/`_shape_fetch`) so the driver is sourced from the job/action, not a constant.
  4. `tab_id` stays an opaque handle; for cloak it's a page-registry key (an int id we assign), NOT a chrome tab id.
- **Availability / fail-loud:** if Chromium/profile can't launch (not installed, profile locked), `health.drivers.cloak.available=false` and every `driver="cloak"` call returns `{status:"error", driver:"cloak", error:"cloak browser unavailable: <reason>"}`. The relay driver is unaffected. NEVER hang, never silent-empty.
- **`/health` gains a `drivers` sub-structure** reconciling the Phase-3 note: `drivers:{relay:{extension_connected,extension_status,last_poll_age_seconds}, cloak:{available, profile_path, pages_open}}`. Keep the existing top-level relay fields for backward-compat (popup/acceptance script read them) AND add `drivers`.
- **Concurrency:** cloak fetches up to `FETCH_CAP` pages in parallel (one Playwright page each); cloak searches near-serial like relay (reuse a single search page). Bounded by an asyncio.Semaphore in the driver.
- **Backward compat:** all Phase 1–3 behavior, shapes, and tests remain green. `driver="relay"` is unchanged and remains the default.

---

### Task 1: Generalize the driver seams (driver on Action, sourced not hardcoded) — relay-only, no cloak yet

**Files:**
- Modify: `server/browser_relay/app.py`
- Modify: `server/tests/test_escalation.py`

**Interfaces:**
- Consumes: Phase 3 `Action`, `_register_action`, `_action_required_payload`, `_shape_*`, `resume`, `health`.
- Produces:
  - `Action.__init__(self, kind, payload, tab_id, action, driver="relay")` — new `driver` field.
  - `_register_action(job, action, tab_id, driver="relay")` threads driver.
  - `_action_required_payload`, `health.pending_actions`, `_shape_search`/`_shape_fetch` source `driver` from the action/job instead of the literal `"relay"`.
  - `Job` gains an optional `driver` attribute (default `"relay"`) so `_shape_*` can read it.
  - Behavior is IDENTICAL for the relay path (driver is still "relay" everywhere) — this task is a pure refactor enabling Task 4's cloak values, verified by the unchanged-green suite.

- [ ] **Step 1: Write the failing test (driver is now parameterized, defaults preserved)**

Append to `server/tests/test_escalation.py`:
```python
async def test_action_carries_driver_field_default_relay(client):
    from browser_relay.app import Action
    a = Action("search", {"query": "q", "engine": "bing"}, 1, "solve_captcha")
    assert a.driver == "relay"
    b = Action("fetch", {"url": "u"}, 2, "login", driver="cloak")
    assert b.driver == "cloak"


async def test_action_required_payload_reflects_action_driver(client):
    # A cloak-registered action must surface driver:"cloak", not a hardcoded relay.
    import browser_relay.app as appmod
    from browser_relay.app import Action, _action_required_payload
    rec = Action("fetch", {"url": "https://x"}, 5, "login", driver="cloak")
    payload = _action_required_payload(rec)
    assert payload["driver"] == "cloak"
    assert payload["status"] == "action_required"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_escalation.py -k "driver" -v`
Expected: FAIL — `Action.__init__` takes no `driver`; `_action_required_payload` hardcodes `"relay"`.

- [ ] **Step 3: Add `driver` to `Action` and `Job`**

In `app.py`, update `Action`:
```python
class Action:
    __slots__ = ("resume_token", "kind", "payload", "tab_id", "action", "created_at", "resolved", "driver")

    def __init__(self, kind: str, payload: dict, tab_id, action: str, driver: str = "relay"):
        self.resume_token = secrets.token_urlsafe(12)
        self.kind = kind
        self.payload = payload
        self.tab_id = tab_id
        self.action = action
        self.created_at = time.monotonic()
        self.resolved = False
        self.driver = driver
```
Update `Job` to carry a driver (add to `__slots__` and `__init__`):
```python
class Job:
    __slots__ = ("job_id", "kind", "payload", "event", "result", "dispatched", "driver")

    def __init__(self, kind: str, payload: dict, driver: str = "relay"):
        self.job_id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.payload = payload
        self.event = asyncio.Event()
        self.result = None
        self.dispatched = False
        self.driver = driver
```

- [ ] **Step 4: Source driver from action/job instead of the literal**

`_register_action`:
```python
def _register_action(job, action: str, tab_id, driver: str = "relay") -> Action:
    record = Action(job.kind, dict(job.payload), tab_id, action, driver=driver)
    actions[record.resume_token] = record
    return record
```
`_action_required_payload` — replace `"driver": "relay"` with `"driver": record.driver`.
`_shape_search`/`_shape_fetch` — replace `"driver": "relay"` in the `base` dict with `"driver": job.driver`.
`health.pending_actions` — replace `"driver": "relay"` with `"driver": a.driver`.
`post_result` — pass the job's driver when registering: `_register_action(job, data.get("action","solve_captcha"), data.get("tab_id"), driver=job.driver)`.

- [ ] **Step 5: Run to verify it passes + full suite unchanged**

Run: `cd server && uv run pytest tests/test_escalation.py -v && uv run pytest`
Expected: new tests pass; ALL prior tests green (relay path still emits driver:"relay" because Jobs/Actions default to relay).

- [ ] **Step 6: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_escalation.py
git commit -m "refactor(relay): source driver from Job/Action (generalize seam for cloak)"
```

---

### Task 2: `CloakDriver` — launch, availability, search, fetch (shared-JS injection)

**Files:**
- Create: `server/browser_relay/drivers/__init__.py`
- Create: `server/browser_relay/drivers/cloak.py`
- Modify: `server/pyproject.toml` (add patchright dep)
- Create: `server/tests/test_cloak_driver.py`

**Interfaces:**
- Consumes: the shared JS files in `extension/`.
- Produces:
  - `class CloakDriver` with `async def start()`, `async def search(query, k, engine) -> dict`, `async def fetch(url, include_html) -> dict`, `async def close()`, and `available: bool` / `def status() -> dict`.
  - Module-level `get_cloak_driver() -> CloakDriver` singleton accessor.
  - `_SHARED_JS_DIR` resolution that finds the `extension/` dir relative to the repo (env override `BROWSER_RELAY_SHARED_JS_DIR`).
  - Results in the SAME shape as relay (`{status, driver:"cloak", ...}`), incl. `action_required` on block/login.

- [ ] **Step 1: Add patchright dependency**

In `server/pyproject.toml`, add to `dependencies`:
```toml
    "patchright>=1.40",
```
Run:
```bash
cd /Users/bkumara/personal/localvily/server && uv sync 2>&1 | tail -3
uv run patchright install chromium 2>&1 | tail -3 || echo "CHROMIUM_INSTALL_FAILED (tests will skip browser-dependent cases)"
```
Expected: patchright installs. The `patchright install chromium` downloads a browser (~150MB); if it fails (offline/CI), note it — the browser-dependent tests skip gracefully.

- [ ] **Step 2: Write the failing tests (availability + shared-JS resolution, no browser needed)**

`server/tests/test_cloak_driver.py`:
```python
import os
from pathlib import Path

import pytest

from browser_relay.drivers.cloak import CloakDriver, _shared_js_dir


def test_shared_js_dir_resolves_to_extension_with_inject_files():
    d = _shared_js_dir()
    assert (d / "inject" / "serp.js").exists(), f"serp.js not found under {d}"
    assert (d / "inject" / "extract.js").exists()
    assert (d / "lib" / "Readability.js").exists()


def test_driver_status_before_start_reports_unavailable():
    drv = CloakDriver()
    st = drv.status()
    assert st["available"] is False
    assert "profile_path" in st


def _chromium_available() -> bool:
    # patchright/playwright stores browsers under ms-playwright cache
    try:
        from patchright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False
    return os.environ.get("BROWSER_RELAY_RUN_CLOAK_TESTS") == "1"


@pytest.mark.skipif(not _chromium_available(),
                    reason="set BROWSER_RELAY_RUN_CLOAK_TESTS=1 with chromium installed to run live cloak tests")
async def test_cloak_fetch_real_page_returns_text():
    drv = CloakDriver()
    await drv.start()
    try:
        result = await drv.fetch("https://en.wikipedia.org/wiki/Consistent_hashing", include_html=False)
        assert result["status"] == "ok"
        assert result["driver"] == "cloak"
        assert result["length"] >= 1000
    finally:
        await drv.close()


@pytest.mark.skipif(not _chromium_available(),
                    reason="set BROWSER_RELAY_RUN_CLOAK_TESTS=1 with chromium installed to run live cloak tests")
async def test_cloak_search_real_returns_results():
    drv = CloakDriver()
    await drv.start()
    try:
        result = await drv.search("consistent hashing", k=10, engine="bing")
        assert result["status"] in ("ok", "action_required")
        if result["status"] == "ok":
            assert result["count"] >= 1
            assert result["driver"] == "cloak"
    finally:
        await drv.close()
```

- [ ] **Step 2b: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_cloak_driver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'browser_relay.drivers.cloak'` (the two non-browser tests are the ones that must pass after implementation; the two live tests skip).

- [ ] **Step 3: Implement the driver**

`server/browser_relay/drivers/__init__.py`: (empty)

`server/browser_relay/drivers/cloak.py`:
```python
"""Embedded stealth-browser driver (patchright). Self-contained — no external MCP.

Reuses the SAME in-page JS as the Chrome extension: extension/inject/serp.js
(globalThis.__serp) and extension/lib/Readability.js + extension/inject/extract.js
(globalThis.__extract, globalThis.__detectLogin), so parsing/extraction has one
source of truth across both drivers.
"""
import asyncio
import os
from pathlib import Path

CLOAK_PROFILE_DIR = os.environ.get(
    "BROWSER_RELAY_CLOAK_PROFILE_DIR",
    str(Path(__file__).resolve().parents[3] / ".cloak-profile"),
)
FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_RELAY_CLOAK_NAV_TIMEOUT_MS", "20000"))
SETTLE_MS = int(os.environ.get("BROWSER_RELAY_FETCH_SETTLE_MS", "800"))


def _shared_js_dir() -> Path:
    override = os.environ.get("BROWSER_RELAY_SHARED_JS_DIR")
    if override:
        return Path(override)
    # repo_root/server/browser_relay/drivers/cloak.py -> repo_root/extension
    return Path(__file__).resolve().parents[3] / "extension"


def _serp_url(query: str, k: int) -> str:
    from urllib.parse import quote_plus
    count = max(1, min(k, 50))
    return f"https://www.bing.com/search?q={quote_plus(query)}&count={count}"


class CloakDriver:
    def __init__(self):
        self._pw = None
        self._ctx = None
        self.available = False
        self._error = None
        self._search_page = None
        self._sem = asyncio.Semaphore(FETCH_CAP)
        self._js = {}
        d = _shared_js_dir()
        for key, rel in (("serp", "inject/serp.js"), ("readability", "lib/Readability.js"),
                         ("extract", "inject/extract.js")):
            p = d / rel
            self._js[key] = p.read_text(encoding="utf-8") if p.exists() else ""

    def status(self) -> dict:
        return {
            "available": self.available,
            "profile_path": CLOAK_PROFILE_DIR,
            "pages_open": 0 if not self._ctx else len(self._ctx.pages),
            **({"error": self._error} if self._error else {}),
        }

    async def start(self):
        if self.available:
            return
        try:
            from patchright.async_api import async_playwright
            self._pw = await async_playwright().start()
            Path(CLOAK_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
            launch_kwargs = dict(user_data_dir=CLOAK_PROFILE_DIR, headless=False)
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            self.available = True
            self._error = None
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self.available = False
            await self._cleanup()

    async def _cleanup(self):
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        self._search_page = None

    async def close(self):
        await self._cleanup()
        self.available = False

    async def _eval_serp(self, page, k: int) -> dict:
        await page.add_script_tag(content=self._js["serp"])
        return await page.evaluate(
            "(k) => globalThis.__serp.detectBlock(document) ? {blocked:true}"
            " : {results: globalThis.__serp.parse(document, k)}",
            k,
        )

    async def _eval_extract(self, page) -> dict:
        await page.add_script_tag(content=self._js["readability"])
        await page.add_script_tag(content=self._js["extract"])
        return await page.evaluate(
            "() => ({login: globalThis.__detectLogin(document), content: globalThis.__extract(document)})"
        )

    def _unavailable(self) -> dict:
        return {"status": "error", "driver": "cloak",
                "error": f"cloak browser unavailable: {self._error or 'not started'}"}

    async def search(self, query: str, k: int = 10, engine: str = "bing") -> dict:
        if not self.available:
            await self.start()
        if not self.available:
            return self._unavailable()
        base = {"query": query, "engine": engine, "driver": "cloak"}
        try:
            page = await self._ctx.new_page()
            await page.goto(_serp_url(query, k), timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(SETTLE_MS)
            res = await self._eval_serp(page, k)
            if res.get("blocked"):
                await page.bring_to_front()
                return {"status": "action_required", **base, "action": "solve_captcha",
                        "_page": page}  # _page handed to the relay's action registry
            await page.close()
            results = res.get("results", [])
            return {"status": "ok", **base, "count": len(results), "results": results}
        except Exception as exc:
            return {"status": "error", **base, "error": f"{type(exc).__name__}: {exc}"}

    async def fetch(self, url: str, include_html: bool = False) -> dict:
        if not self.available:
            await self.start()
        if not self.available:
            return self._unavailable()
        base = {"url": url, "driver": "cloak"}
        async with self._sem:
            try:
                page = await self._ctx.new_page()
                await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(SETTLE_MS)
                res = await self._eval_extract(page)
                if res.get("login"):
                    await page.bring_to_front()
                    return {"status": "action_required", **base, "action": "login", "_page": page}
                content = res.get("content") or {}
                if not content.get("text"):
                    await page.close()
                    return {"status": "error", **base, "error": "no extractable content"}
                if include_html:
                    content = {**content, "html": await page.content()}
                await page.close()
                return {"status": "ok", **base, **{k: content[k] for k in
                        ("title", "text", "excerpt", "length", "html") if k in content}}
            except Exception as exc:
                return {"status": "error", **base, "error": f"{type(exc).__name__}: {exc}"}

    async def recheck(self, page, kind: str, payload: dict) -> dict:
        """Re-evaluate a held page after the human acted (resume path)."""
        if kind == "search":
            base = {"query": payload.get("query"), "engine": payload.get("engine"), "driver": "cloak"}
            res = await self._eval_serp(page, payload.get("k", 10))
            if res.get("blocked"):
                await page.bring_to_front()
                return {"status": "action_required", **base, "action": "solve_captcha", "_page": page}
            await page.close()
            results = res.get("results", [])
            return {"status": "ok", **base, "count": len(results), "results": results}
        base = {"url": payload.get("url"), "driver": "cloak"}
        res = await self._eval_extract(page)
        if res.get("login"):
            await page.bring_to_front()
            return {"status": "action_required", **base, "action": "login", "_page": page}
        content = res.get("content") or {}
        if not content.get("text"):
            await page.close()
            return {"status": "error", **base, "error": "no extractable content"}
        await page.close()
        return {"status": "ok", **base, **{k: content[k] for k in
                ("title", "text", "excerpt", "length") if k in content}}


_driver: CloakDriver | None = None


def get_cloak_driver() -> CloakDriver:
    global _driver
    if _driver is None:
        _driver = CloakDriver()
    return _driver
```

- [ ] **Step 4: Run to verify the non-browser tests pass**

Run: `cd server && uv run pytest tests/test_cloak_driver.py -v`
Expected: the two non-browser tests PASS; the two live tests SKIP (no `BROWSER_RELAY_RUN_CLOAK_TESTS=1`). Full suite still green.

- [ ] **Step 5: Commit**

```bash
git add server/browser_relay/drivers/ server/pyproject.toml server/tests/test_cloak_driver.py
git commit -m "feat(cloak): embedded patchright driver (search/fetch via shared JS, availability, recheck)"
```

---

### Task 3: Route `driver="cloak"` through the relay + cloak action registration + `/health` drivers

**Files:**
- Modify: `server/browser_relay/app.py`
- Create: `server/tests/test_cloak_routing.py`

**Interfaces:**
- Consumes: `get_cloak_driver()`, the action registry, the seam-generalized `_register_action`.
- Produces:
  - `/search` + `/fetch` with `driver="cloak"` → call the cloak driver in-process; on `action_required`, register a cloak action holding the page (the `_page` from the driver result) under an integer `tab_id` page-handle.
  - A `cloak_pages: dict[int, page]` registry + `_next_cloak_page_id` counter (since the action stores an int `tab_id`, not a real page object, to keep `Action` JSON-friendly).
  - `/health` gains `drivers:{relay:{...}, cloak: driver.status()}` while keeping existing top-level relay fields.
  - These tests MONKEYPATCH `get_cloak_driver` with a fake async driver (no real browser), so routing/registration is testable headlessly.

- [ ] **Step 1: Write the failing tests (fake cloak driver)**

`server/tests/test_cloak_routing.py`:
```python
import browser_relay.app as appmod


class FakeCloakDriver:
    def __init__(self):
        self.available = True
        self.searched = []

    def status(self):
        return {"available": True, "profile_path": "/tmp/x", "pages_open": 0}

    async def start(self):
        self.available = True

    async def search(self, query, k=10, engine="bing"):
        self.searched.append(query)
        if query == "BLOCKME":
            return {"status": "action_required", "query": query, "engine": engine,
                    "driver": "cloak", "action": "solve_captcha", "_page": object()}
        return {"status": "ok", "query": query, "engine": engine, "driver": "cloak",
                "count": 1, "results": [{"title": "T", "url": "https://e", "snippet": "s"}]}

    async def fetch(self, url, include_html=False):
        return {"status": "ok", "url": url, "driver": "cloak",
                "title": "D", "text": "y" * 1200, "excerpt": "e", "length": 1200}


def setup_function():
    appmod.actions.clear()
    appmod.cloak_pages.clear()


async def test_cloak_search_routes_to_driver(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    resp = await client.get("/search", params={"q": "hello", "driver": "cloak"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] == "cloak"
    assert body["count"] == 1
    assert fake.searched == ["hello"]


async def test_cloak_fetch_routes_to_driver(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    resp = await client.get("/fetch", params={"url": "https://x", "driver": "cloak"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] == "cloak"
    assert body["length"] == 1200


async def test_cloak_block_registers_cloak_action_with_page(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    resp = await client.get("/search", params={"q": "BLOCKME", "driver": "cloak"})
    body = resp.json()
    assert body["status"] == "action_required"
    assert body["driver"] == "cloak"
    token = body["resume_token"]
    action = appmod.actions[token]
    assert action.driver == "cloak"
    # the page is held in the cloak_pages registry under the action's tab_id
    assert action.tab_id in appmod.cloak_pages
    # the action_required payload must NOT leak the raw _page object
    assert "_page" not in body


async def test_health_has_drivers_substructure(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    body = (await client.get("/health")).json()
    assert "drivers" in body
    assert "relay" in body["drivers"]
    assert "cloak" in body["drivers"]
    assert body["drivers"]["cloak"]["available"] is True
    # backward-compat: top-level relay fields still present
    assert "extension_connected" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_cloak_routing.py -v`
Expected: FAIL — `cloak_pages` missing; `/search?driver=cloak` returns the old "cloak driver not available in this build" error; `/health` has no `drivers`.

- [ ] **Step 3: Add the cloak page registry + import**

In `app.py`, near the `actions` registry:
```python
from browser_relay.drivers.cloak import get_cloak_driver

cloak_pages: dict = {}          # int handle -> live cloak page object
_next_cloak_page_id: int = 1


def _register_cloak_action(driver_result: dict, kind: str, payload: dict) -> dict:
    """Turn a cloak driver action_required result (carrying _page) into a registered
    action + a JSON-safe payload. Stores the page under an int handle."""
    global _next_cloak_page_id
    page = driver_result.pop("_page", None)
    handle = _next_cloak_page_id
    _next_cloak_page_id += 1
    cloak_pages[handle] = page
    job = Job(kind, dict(payload), driver="cloak")
    record = _register_action(job, driver_result.get("action", "solve_captcha"), handle, driver="cloak")
    return _action_required_payload(record)
```

- [ ] **Step 4: Route cloak in `/search` and `/fetch`**

Replace the `driver != "relay"` rejection in `search()`:
```python
@app.get("/search")
async def search(q: str, k: int = 10, engine: str = "bing", driver: str = "relay"):
    if not q.strip():
        raise HTTPException(400, "query is required")
    if driver == "cloak":
        result = await get_cloak_driver().search(q.strip(), k=k, engine=engine)
        if result.get("status") == "action_required":
            return _register_cloak_action(result, "search", {"query": q.strip(), "k": k, "engine": engine})
        return result
    job = Job("search", {"query": q.strip(), "k": k, "engine": engine})
    await _await_job(job, search_queue, QUERY_TIMEOUT)
    return _shape_search(job)
```
And `fetch()`:
```python
@app.get("/fetch")
async def fetch(url: str, include_html: bool = False, driver: str = "relay"):
    if not url.strip():
        raise HTTPException(400, "url is required")
    if driver == "cloak":
        result = await get_cloak_driver().fetch(url.strip(), include_html=include_html)
        if result.get("status") == "action_required":
            return _register_cloak_action(result, "fetch", {"url": url.strip(), "include_html": include_html})
        return result
    job = Job("fetch", {"url": url.strip(), "include_html": include_html})
    await _await_job(job, fetch_queue, FETCH_TIMEOUT)
    return _shape_fetch(job)
```
(Note: an unknown driver other than "relay"/"cloak" now falls through to the relay path. If you want to keep rejecting unknown drivers, add `elif driver != "relay": return {"status":"error","error":f"unknown driver: {driver}"}` — DO add this guard for fail-loud.)

- [ ] **Step 5: Add `drivers` to `/health`**

In `health()`, build the relay sub-dict from existing values and add cloak status:
```python
    cloak_status = get_cloak_driver().status()
    # ... existing return dict, ADD:
    #   "drivers": {
    #       "relay": {"extension_connected": connected, "extension_status": <same>,
    #                 "last_poll_age_seconds": poll_age},
    #       "cloak": cloak_status,
    #   },
```
Keep all existing top-level keys for backward-compat.

- [ ] **Step 6: Run to verify pass + full suite**

Run: `cd server && uv run pytest tests/test_cloak_routing.py -v && uv run pytest`
Expected: 4 new tests pass; full suite green (relay path untouched).

- [ ] **Step 7: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_cloak_routing.py
git commit -m "feat(cloak): route driver=cloak through relay, register cloak actions, health.drivers"
```

---

### Task 4: Fork `resume()` by driver (cloak re-drives the held page) + cloak resume tool path

**Files:**
- Modify: `server/browser_relay/app.py`
- Modify: `server/tests/test_cloak_routing.py`

**Interfaces:**
- Consumes: `cloak_pages`, `get_cloak_driver().recheck`, the seam-generalized `resume`.
- Produces: `resume(token)` forks on `record.driver`: `relay` → existing recheck-job path; `cloak` → look up the held page in `cloak_pages`, call `get_cloak_driver().recheck(page, kind, payload)`, then mirror the relay logic (still-blocked → keep token + refresh TTL + re-register the new page handle; ok/error → resolve + drop + discard the page handle).

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_cloak_routing.py`:
```python
class ReCheckCloakDriver(FakeCloakDriver):
    def __init__(self, recheck_results):
        super().__init__()
        self._recheck_results = list(recheck_results)

    async def recheck(self, page, kind, payload):
        r = self._recheck_results.pop(0)
        return r


async def test_cloak_resume_clears_and_resolves(client, monkeypatch):
    # First a block, then resume → recheck returns ok.
    fake = ReCheckCloakDriver([
        {"status": "ok", "url": "https://x", "driver": "cloak",
         "title": "D", "text": "z" * 1100, "length": 1100},
    ])
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)

    # Block first (fetch login).
    monkeypatch.setattr(fake, "fetch", _blocking_fetch)
    resp = await client.get("/fetch", params={"url": "https://x", "driver": "cloak"})
    token = resp.json()["resume_token"]
    assert resp.json()["status"] == "action_required"
    assert appmod.actions[token].driver == "cloak"

    # Resume → recheck ok → resolved + page handle freed.
    rresp = await client.post(f"/resume/{token}")
    rbody = rresp.json()
    assert rbody["status"] == "ok"
    assert rbody["driver"] == "cloak"
    assert token not in appmod.actions
    again = (await client.post(f"/resume/{token}")).json()
    assert again["status"] == "error"


async def _blocking_fetch(url, include_html=False):
    return {"status": "action_required", "url": url, "driver": "cloak",
            "action": "login", "_page": object()}


async def test_cloak_resume_still_blocked_keeps_token(client, monkeypatch):
    fake = ReCheckCloakDriver([
        {"status": "action_required", "url": "https://x", "driver": "cloak",
         "action": "login", "_page": object()},
        {"status": "ok", "url": "https://x", "driver": "cloak", "title": "D",
         "text": "z" * 1100, "length": 1100},
    ])
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    monkeypatch.setattr(fake, "fetch", _blocking_fetch)

    resp = await client.get("/fetch", params={"url": "https://x", "driver": "cloak"})
    token = resp.json()["resume_token"]

    r1 = (await client.post(f"/resume/{token}")).json()
    assert r1["status"] == "action_required"
    assert r1["resume_token"] == token        # same token kept
    assert token in appmod.actions
    assert len(appmod.actions) == 1

    r2 = (await client.post(f"/resume/{token}")).json()
    assert r2["status"] == "ok"
    assert token not in appmod.actions
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_cloak_routing.py -k "resume" -v`
Expected: FAIL — `resume` always uses the relay recheck-job path; a cloak token has no relay job served, so it times out / mis-handles.

- [ ] **Step 3: Fork `resume()` by driver**

In `app.py`, modify `resume()` after the unknown/resolved/expired guards, replace the single relay path with a fork:
```python
    if record.driver == "cloak":
        page = cloak_pages.pop(record.tab_id, None)
        if page is None:
            actions.pop(token, None)
            return {"status": "error", "error": "cloak page no longer available"}
        result = await get_cloak_driver().recheck(page, record.kind, dict(record.payload))
        if result.get("status") == "action_required":
            # Still blocked: re-hold the (possibly same) page under a fresh handle,
            # keep the ORIGINAL token, refresh TTL.
            new_payload = _register_cloak_action(result, record.kind, dict(record.payload))
            new_token = new_payload["resume_token"]
            actions[token] = actions.pop(new_token)
            actions[token].resume_token = token
            actions[token].created_at = time.monotonic()
            new_payload["resume_token"] = token
            return new_payload
        record.resolved = True
        actions.pop(token, None)
        return result

    # --- relay path (existing) ---
    payload = dict(record.payload)
    payload["recheck_tab_id"] = record.tab_id
    job = Job(record.kind, payload)
    ...
```
(Keep the existing relay block intact below the cloak fork.)

- [ ] **Step 4: Run to verify pass + full suite**

Run: `cd server && uv run pytest tests/test_cloak_routing.py -v && uv run pytest`
Expected: cloak resume tests pass; full suite green.

- [ ] **Step 5: Commit**

```bash
git add server/browser_relay/app.py server/tests/test_cloak_routing.py
git commit -m "feat(cloak): fork resume by driver — cloak re-drives held page in-process"
```

---

### Task 5: MCP tool docs + acceptance note + lifespan cloak cleanup

**Files:**
- Modify: `server/browser_relay/mcp_server/server.py` (tool docstrings mention cloak is now available)
- Modify: `server/browser_relay/app.py` (lifespan closes the cloak driver on shutdown)
- Modify: `tests/acceptance/run_acceptance.py` (optional cloak smoke when enabled)
- Create: `server/tests/test_cloak_lifespan.py`

**Interfaces:**
- Consumes: `get_cloak_driver()`.
- Produces: clean cloak shutdown in the lifespan; updated tool docs; a guarded cloak acceptance check.

- [ ] **Step 1: Update tool docstrings**

In `server/browser_relay/mcp_server/server.py`, update the `search`/`fetch`/`search_and_fetch` docstrings so `driver` reads:
`driver: "relay" (default, the user's logged-in Chrome) or "cloak" (embedded stealth browser for bot-protected/unattended pages).`

- [ ] **Step 2: Close the cloak driver on shutdown (failing test)**

`server/tests/test_cloak_lifespan.py`:
```python
import browser_relay.app as appmod


async def test_lifespan_closes_cloak_driver(monkeypatch):
    closed = {"v": False}

    class FakeDrv:
        available = True
        async def close(self):
            closed["v"] = True
        def status(self):
            return {"available": True, "profile_path": "x", "pages_open": 0}

    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: FakeDrv())
    # Exercise the lifespan context manager directly.
    async with appmod._lifespan(appmod.app):
        pass
    assert closed["v"] is True
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_cloak_lifespan.py -v`
Expected: FAIL — lifespan doesn't close the cloak driver.

- [ ] **Step 4: Add cloak cleanup to the lifespan**

In `_lifespan`, in the `finally`, after cancelling the sweep task:
```python
    finally:
        task.cancel()
        try:
            await get_cloak_driver().close()
        except Exception:
            pass
```

- [ ] **Step 5: Add a guarded cloak acceptance check**

In `tests/acceptance/run_acceptance.py`, after the relay criteria, add (only runs when env `BROWSER_RELAY_ACCEPT_CLOAK=1`):
```python
        import os
        if os.environ.get("BROWSER_RELAY_ACCEPT_CLOAK") == "1":
            cf = (await c.get("/fetch", params={"url": "https://en.wikipedia.org/wiki/Consistent_hashing", "driver": "cloak"})).json()
            print(f"C6 cloak fetch length={cf.get('length')} status={cf.get('status')}")
            if cf.get("status") != "ok" or cf.get("length", 0) < 1000:
                failures.append(f"C6 cloak fetch: expected >=1000 chars ok, got {cf}")
```

- [ ] **Step 6: Run to verify pass + full suite**

Run: `cd server && uv run pytest tests/test_cloak_lifespan.py -v && uv run pytest`
Expected: pass; full suite green.

- [ ] **Step 7: Commit**

```bash
git add server/browser_relay/mcp_server/server.py server/browser_relay/app.py server/tests/test_cloak_lifespan.py tests/acceptance/run_acceptance.py
git commit -m "feat(cloak): close driver on shutdown, tool docs, guarded cloak acceptance check"
```

---

## Self-Review

**1. Spec coverage (spec §8 cloak driver + §3 drivers + Phase 3 carry-forward):**
- Embedded patchright persistent context, headed-bg, profile dir → Task 2. ✓
- Shared JS (serp/Readability/extract) evaluated in-page, single source of truth → Task 2 (`_eval_serp`/`_eval_extract`). ✓
- `driver="cloak"` routing on search/fetch, same result shapes → Task 3. ✓
- Escalation via bring_to_front + cloak action registry → Task 3; resume re-drives held page → Task 4. ✓
- Availability/fail-loud (browser won't launch → error, relay unaffected) → Task 2 `_unavailable` + Task 3 routing. ✓
- `health.drivers{relay,cloak}` → Task 3. ✓
- The 4 Phase-3 seams: driver on Action/Job (Task 1), resume fork (Task 4), 3 hardcoded strings replaced (Task 1), tab_id opaque handle = int page key (Task 3). ✓
- Login persistence via profile dir: inherent to persistent context (no code — the profile survives restarts). ✓

**2. Placeholder scan:** all code blocks complete. The live browser tests are explicitly skip-guarded (not placeholders). Task 3 Step 4 notes the unknown-driver guard to ADD (concrete). No TBD.

**3. Type consistency:** `CloakDriver.search/fetch/recheck/status/start/close`, `get_cloak_driver()`, `cloak_pages`, `_register_cloak_action`, `Action.driver`, `Job.driver`, `_register_action(..., driver=)` consistent across tasks. The `_page` key convention (driver result carries it; relay strips it via `_register_cloak_action` before JSON) is consistent and tested (`"_page" not in body`).

**4. Headless-env reality:** Tasks 1, 3, 4, 5 are FULLY testable headlessly (fake driver / direct units). Task 2's two live tests skip without `BROWSER_RELAY_RUN_CLOAK_TESTS=1` + chromium. The real stealth/CAPTCHA behavior is validated in an attended env — documented, like Phase 2's live acceptance.

---

## Note for Plan 5 (packaging)
- `pyproject.toml` now depends on patchright; packaging must document `patchright install chromium` as a post-install step (or lazy-install note).
- `.cloak-profile/` is gitignored (add in Plan 5 if not already).
- `/health` shape now has `drivers{}` — the popup (Plan 5 polish) can show both drivers' status.
