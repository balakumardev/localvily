import asyncio

import httpx
import pytest
from httpx import ASGITransport

from browser_relay.app import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def drive_extension(client, respond, *, polls=200, delay=0.01, idle_limit=3):
    """Simulate the browser extension: poll /pending, post a result for each job.

    `respond(job)` returns the JSON body to POST to /result/{job_id}.
    Stops after it has answered at least one job and has then seen `idle_limit`
    consecutive empty polls. The idle grace lets a caller that enqueues work in
    waves (e.g. search_and_fetch: a search job followed by parallel fetch jobs)
    resume and enqueue the next wave before the simulated extension gives up.
    """
    answered = 0
    idle = 0
    for _ in range(polls):
        r = await client.get("/pending")
        jobs = r.json()["jobs"]
        if not jobs:
            if answered:
                idle += 1
                if idle >= idle_limit:
                    return
            await asyncio.sleep(delay)
            continue
        idle = 0
        for job in jobs:
            await client.post(f"/result/{job['job_id']}", json=respond(job))
            answered += 1
