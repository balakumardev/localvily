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

import os

FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
SEARCH_CONCURRENCY = int(os.environ.get("BROWSER_RELAY_SEARCH_CONCURRENCY", "1"))
SEARCH_MIN_SPACING_MS = int(os.environ.get("BROWSER_RELAY_SEARCH_MIN_SPACING_MS", "500"))

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
        _release(job)


@app.get("/version")
async def version():
    return {"version": __version__}


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
