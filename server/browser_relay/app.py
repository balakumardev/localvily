import asyncio
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from browser_relay import __version__

EXTENSION_RECENT_POLL_THRESHOLD = 75.0  # seconds
QUERY_TIMEOUT = 110.0
FETCH_TIMEOUT = 60.0

import os
import secrets

FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
SEARCH_CONCURRENCY = int(os.environ.get("BROWSER_RELAY_SEARCH_CONCURRENCY", "1"))
SEARCH_MIN_SPACING_MS = int(os.environ.get("BROWSER_RELAY_SEARCH_MIN_SPACING_MS", "500"))
ACTION_TTL = float(os.environ.get("BROWSER_RELAY_ACTION_TTL", "300"))

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

action_close_queue: deque = deque()  # tab ids to close (expired/resolved actions)


async def _sweep_expired_actions():
    now = time.monotonic()
    expired = [t for t, a in actions.items() if not a.resolved and (now - a.created_at) >= ACTION_TTL]
    for token in expired:
        record = actions.pop(token, None)
        if record and record.tab_id is not None:
            action_close_queue.append(record.tab_id)


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


app = FastAPI(lifespan=_lifespan)


class ResultBody(BaseModel):
    model_config = {"extra": "allow"}


def _extension_connected() -> bool:
    if last_poll_time == 0:
        return False
    return (time.monotonic() - last_poll_time) <= EXTENSION_RECENT_POLL_THRESHOLD


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


def _shape_search(job: Job) -> dict:
    result = job.result or {"error": "no result"}
    if result.get("status") == "action_required":
        return result  # already shaped by post_result
    base = {
        "query": job.payload["query"],
        "engine": job.payload["engine"],
        "driver": "relay",
    }
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
        _release(job)


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
            "driver": "relay",
            "since_seconds": round(now_m - a.created_at, 1),
            **({"query": a.payload.get("query")} if a.kind == "search" else {"url": a.payload.get("url")}),
        }
        for a in actions.values()
        if not a.resolved
    ]
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
        "pending_actions": pending_actions,
        "version": __version__,
    }


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
    global last_poll_time, search_in_flight, fetch_in_flight, last_search_dispatch
    last_poll_time = time.monotonic()
    now = time.monotonic()
    batch = []

    # Searches: near-serial with spacing. Recheck jobs reuse a held tab (they do
    # not open a new search tab against the engine), so they bypass the spacing throttle.
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

    return {"jobs": batch, "close_tabs": close_tabs}


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
        record = _register_action(job, data.get("action", "solve_captcha"), data.get("tab_id"))
        job.result = _action_required_payload(record)
    else:
        job.result = data
    _release(job)
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
