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
