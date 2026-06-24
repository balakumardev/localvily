import asyncio
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from browser_relay import __version__
from browser_relay.drivers.cloak import get_cloak_driver

EXTENSION_RECENT_POLL_THRESHOLD = 75.0  # seconds
QUERY_TIMEOUT = 110.0
FETCH_TIMEOUT = 60.0

import os
import secrets

# Parallel fetch tabs. Fetches hit arbitrary article domains (not the search
# engine), so unlike searches they carry no CAPTCHA risk from concurrency — raise
# BROWSER_RELAY_FETCH_CAP for heavier fetch fan-out (e.g. multi-threaded STORM).
FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
# Concurrent SERP tabs. STORM fans out dozens of searches at once; serializing
# them (the old default of 1) made each call queue behind the others and balloon
# to 20-75s observed latency. Running a few SERP tabs in parallel collapses that
# queue. Kept modest — and dispatched with a small stagger (below) — so we don't
# trip Bing's bot detection. Tune via env if a given session starts seeing CAPTCHAs.
SEARCH_CONCURRENCY = int(os.environ.get("BROWSER_RELAY_SEARCH_CONCURRENCY", "4"))
# Minimum gap between *new* SERP dispatches. Staggers the ramp-up to SEARCH_CONCURRENCY
# instead of opening N tabs in the same instant (gentler on Bing), while still
# reaching full parallelism within a few hundred ms.
SEARCH_MIN_SPACING_MS = int(os.environ.get("BROWSER_RELAY_SEARCH_MIN_SPACING_MS", "200"))
ACTION_TTL = float(os.environ.get("BROWSER_RELAY_ACTION_TTL", "300"))

# Long-poll: the extension calls /pending?wait=N so the relay can hold the request
# open until a job is dispatchable, instead of the extension blind-polling on a
# fixed interval. Capped below the extension's own fetch timeout. Bare /pending
# (no wait) keeps the original immediate-return behavior (tests rely on this).
PENDING_LONGPOLL_MAX = float(os.environ.get("BROWSER_RELAY_PENDING_LONGPOLL_MAX", "27"))
# How often the long-poll re-evaluates dispatchability when nothing woke it. Also
# bounds how long a spacing-throttled search waits before its slot is reconsidered.
PENDING_RECHECK_INTERVAL = 0.25

_ACTION_MESSAGES = {
    "solve_captcha": "The search engine is showing a CAPTCHA. A browser window has been "
                     "opened — please solve the challenge, then call resume with this token.",
    "login": "The page requires sign-in. A browser window has been opened — please log in, "
             "then call resume with this token.",
}

search_in_flight: int = 0
fetch_in_flight: int = 0
last_search_dispatch: float = 0.0


class Job:
    __slots__ = ("job_id", "kind", "payload", "event", "result", "dispatched", "driver")

    def __init__(self, kind: str, payload: dict, driver: str = "relay"):
        self.job_id = uuid.uuid4().hex[:12]
        self.kind = kind  # "search" | "fetch"
        self.payload = payload
        self.event = asyncio.Event()
        self.result: dict | None = None
        self.dispatched = False
        self.driver = driver


jobs: dict[str, Job] = {}
search_queue: deque[Job] = deque()
fetch_queue: deque[Job] = deque()
last_poll_time: float = 0.0

# Wakes any in-progress long-poll on /pending the moment work becomes
# dispatchable (a job is enqueued, or a slot frees on result/timeout), so pickup
# latency is ~0 instead of a poll interval. A modern asyncio.Event binds to the
# running loop lazily, so module-level construction is fine.
_job_event: asyncio.Event = asyncio.Event()


def _notify_pending() -> None:
    """Signal long-pollers that the dispatch state changed."""
    try:
        _job_event.set()
    except RuntimeError:
        # No running loop (e.g. imported in a sync context) — long-poll isn't active.
        pass


class Action:
    __slots__ = ("resume_token", "kind", "payload", "tab_id", "action", "created_at", "resolved", "driver")

    def __init__(self, kind: str, payload: dict, tab_id, action: str, driver: str = "relay"):
        self.resume_token = secrets.token_urlsafe(12)
        self.kind = kind            # "search" | "fetch"
        self.payload = payload      # original job payload (query/engine/k or url)
        self.tab_id = tab_id
        self.action = action        # "solve_captcha" | "login"
        self.created_at = time.monotonic()
        self.resolved = False
        self.driver = driver


actions: dict[str, Action] = {}

action_close_queue: deque = deque()  # tab ids to close (expired/resolved actions)

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


async def _release_action_resources(record):
    """Release the browser resource a paused action was holding, per driver.

    relay: tab_id is a Chrome tab id — queue it for the extension to close.
    cloak: tab_id is an int handle into cloak_pages holding a live patchright
    page — pop and close it directly (it must NOT go to the extension's
    close_tabs, which only understands real Chrome tab ids).
    """
    if record is None or record.tab_id is None:
        return
    if record.driver == "cloak":
        page = cloak_pages.pop(record.tab_id, None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
    else:
        action_close_queue.append(record.tab_id)


async def _sweep_expired_actions():
    now = time.monotonic()
    expired = [t for t, a in actions.items() if not a.resolved and (now - a.created_at) >= ACTION_TTL]
    for token in expired:
        record = actions.pop(token, None)
        await _release_action_resources(record)


@asynccontextmanager
async def _lifespan(app):
    async def _loop():
        while True:
            await asyncio.sleep(30)
            try:
                await _sweep_expired_actions()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A sweep failure must not kill the loop for the app's lifetime.
                pass
    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await get_cloak_driver().close()
        except Exception:
            pass


app = FastAPI(lifespan=_lifespan)


class ResultBody(BaseModel):
    model_config = {"extra": "allow"}


def _extension_connected() -> bool:
    if last_poll_time == 0:
        return False
    return (time.monotonic() - last_poll_time) <= EXTENSION_RECENT_POLL_THRESHOLD


def _register_action(job: "Job", action: str, tab_id, driver: str = "relay") -> Action:
    record = Action(job.kind, dict(job.payload), tab_id, action, driver=driver)
    actions[record.resume_token] = record
    return record


def _action_required_payload(record: Action) -> dict:
    base = {
        "status": "action_required",
        "driver": record.driver,
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


def _shape_search(job: Job) -> dict:
    result = job.result or {"error": "no result"}
    if result.get("status") == "action_required":
        return result  # already shaped by post_result
    base = {
        "query": job.payload["query"],
        "engine": job.payload["engine"],
        "driver": job.driver,
    }
    if "error" in result:
        return {"status": "error", **base, "error": result["error"]}
    results = result.get("results", [])
    return {"status": "ok", **base, "count": len(results), "results": results}


def _shape_fetch(job: Job) -> dict:
    result = job.result or {"error": "no result"}
    if result.get("status") == "action_required":
        return result
    base = {"url": job.payload["url"], "driver": job.driver}
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
    _notify_pending()  # new work — wake any waiting long-poll
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
        _release(job)
        _notify_pending()  # freed a slot (and/or dequeued) — let queued work dispatch


@app.get("/version")
async def version():
    return {"version": __version__}


DEFAULT_ENGINE = os.environ.get("BROWSER_RELAY_DEFAULT_ENGINE", "bing")


@app.get("/health")
async def health():
    poll_age = None if last_poll_time == 0 else round(time.monotonic() - last_poll_time, 1)
    connected = _extension_connected()
    now_m = time.monotonic()
    pending_actions = [
        {
            "resume_token": a.resume_token,
            "action": a.action,
            "driver": a.driver,
            "since_seconds": round(now_m - a.created_at, 1),
            **({"query": a.payload.get("query")} if a.kind == "search" else {"url": a.payload.get("url")}),
        }
        for a in actions.values()
        if not a.resolved
    ]
    extension_status = "connected" if connected else ("stale" if last_poll_time else "never_seen")
    cloak_status = get_cloak_driver().status()
    return {
        "status": "ok",
        "extension_connected": connected,
        "extension_status": extension_status,
        "last_poll_age_seconds": poll_age,
        "search_queued": len(search_queue),
        "fetch_queued": len(fetch_queue),
        "in_flight": search_in_flight + fetch_in_flight,
        "max_fetch_tabs": FETCH_CAP,
        "engine": DEFAULT_ENGINE,
        "pending_actions": pending_actions,
        "version": __version__,
        "drivers": {
            "relay": {
                "extension_connected": connected,
                "extension_status": extension_status,
                "last_poll_age_seconds": poll_age,
            },
            "cloak": cloak_status,
        },
    }


@app.get("/search")
async def search(q: str, k: int = 10, engine: str = "bing", driver: str = "relay"):
    if not q.strip():
        raise HTTPException(400, "query is required")
    if driver == "cloak":
        result = await get_cloak_driver().search(q.strip(), k=k, engine=engine)
        if result.get("status") == "action_required":
            return _register_cloak_action(result, "search", {"query": q.strip(), "k": k, "engine": engine})
        return result
    if driver != "relay":
        return {"status": "error", "error": f"unknown driver: {driver}"}
    job = Job("search", {"query": q.strip(), "k": k, "engine": engine})
    await _await_job(job, search_queue, QUERY_TIMEOUT)
    return _shape_search(job)


@app.get("/fetch")
async def fetch(url: str, include_html: bool = False, driver: str = "relay"):
    if not url.strip():
        raise HTTPException(400, "url is required")
    if driver == "cloak":
        result = await get_cloak_driver().fetch(url.strip(), include_html=include_html)
        if result.get("status") == "action_required":
            return _register_cloak_action(result, "fetch", {"url": url.strip(), "include_html": include_html})
        return result
    if driver != "relay":
        return {"status": "error", "error": f"unknown driver: {driver}"}
    job = Job("fetch", {"url": url.strip(), "include_html": include_html})
    await _await_job(job, fetch_queue, FETCH_TIMEOUT)
    return _shape_fetch(job)


def _build_pending_batch() -> tuple[list, list]:
    """Pull the next dispatchable jobs plus any tabs queued for closing.

    Searches dispatch up to SEARCH_CONCURRENCY with min-spacing between new ones;
    fetches dispatch up to FETCH_CAP in parallel. Mutates the in-flight counters
    and per-job dispatch flags. Synchronous and non-blocking — the long-poll loop
    in /pending calls this repeatedly.
    """
    global search_in_flight, fetch_in_flight, last_search_dispatch
    now = time.monotonic()
    batch = []

    # Searches: capped at SEARCH_CONCURRENCY with spacing between new dispatches.
    # Recheck jobs reuse a held tab (they do not open a new search tab against the
    # engine), so they bypass the spacing throttle.
    while search_queue and search_in_flight < SEARCH_CONCURRENCY:
        job = search_queue[0]
        if job.dispatched:
            break
        is_recheck = "recheck_tab_id" in job.payload
        if not is_recheck and (now - last_search_dispatch) * 1000 < SEARCH_MIN_SPACING_MS:
            break
        job.dispatched = True
        search_in_flight += 1
        if not is_recheck:
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

    close_tabs = []
    while action_close_queue:
        close_tabs.append(action_close_queue.popleft())

    return batch, close_tabs


@app.get("/pending")
async def pending(wait: float = 0.0):
    """Hand the extension its next batch of jobs.

    Bare ``/pending`` returns immediately (the original behavior — tests rely on
    it). ``/pending?wait=N`` long-polls: the relay holds the request open until a
    job is dispatchable or up to N seconds (capped at PENDING_LONGPOLL_MAX),
    waking the instant work arrives. This removes per-poll pickup latency and
    keeps the MV3 service worker warm via a continuously-outstanding request.
    """
    global last_poll_time
    last_poll_time = time.monotonic()

    wait = min(max(wait, 0.0), PENDING_LONGPOLL_MAX)
    deadline = time.monotonic() + wait
    while True:
        batch, close_tabs = _build_pending_batch()
        if batch or close_tabs or wait <= 0:
            return {"jobs": batch, "close_tabs": close_tabs}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"jobs": [], "close_tabs": []}
        # Wait to be woken by new work / a freed slot, but re-check periodically
        # so a spacing-throttled search still dispatches when its window opens.
        _job_event.clear()
        try:
            await asyncio.wait_for(_job_event.wait(), timeout=min(remaining, PENDING_RECHECK_INTERVAL))
        except asyncio.TimeoutError:
            pass
        last_poll_time = time.monotonic()


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
    data = body.model_dump()
    if data.get("action_required"):
        record = _register_action(job, data.get("action", "solve_captcha"), data.get("tab_id"), driver=job.driver)
        job.result = _action_required_payload(record)
    else:
        job.result = data
    _release(job)
    _notify_pending()  # freed a slot — let a queued job dispatch on the next poll
    job.event.set()
    return {"status": "ok"}


@app.post("/resume/{token}")
async def resume(token: str):
    record = actions.get(token)
    if not record:
        return {"status": "error", "error": "action expired or unknown"}
    if record.resolved:
        return {"status": "error", "error": "action already resolved or unknown"}
    if (time.monotonic() - record.created_at) >= ACTION_TTL:
        actions.pop(token, None)
        await _release_action_resources(record)
        return {"status": "error", "error": "action expired"}

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
