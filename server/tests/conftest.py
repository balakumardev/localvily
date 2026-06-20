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
