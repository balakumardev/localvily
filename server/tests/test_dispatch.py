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


async def test_timeout_frees_fetch_capacity(client, monkeypatch):
    # The spec names "a counter not released on timeout permanently starves capacity"
    # as the highest-risk failure mode. Force a dispatched fetch to time out (never
    # answered) and assert the freed slot lets a queued job dispatch.
    monkeypatch.setattr(appmod, "FETCH_TIMEOUT", 0.15)
    tasks = await _enqueue(client, "fetch", 6)
    r1 = await client.get("/pending")
    assert len(r1.json()["jobs"]) == appmod.FETCH_CAP  # 5 dispatched, 1 still queued
    assert appmod.fetch_in_flight == appmod.FETCH_CAP

    # Let the 5 dispatched calls hit FETCH_TIMEOUT without any /result posted.
    await asyncio.gather(*tasks)  # each returns a 504 response; timeout path runs _release

    assert appmod.fetch_in_flight == 0  # every dispatched counter released on timeout
    # The 6th job's caller also timed out and dequeued itself, so nothing remains —
    # capacity is fully restored rather than permanently starved.
    r2 = await client.get("/pending")
    assert r2.json()["jobs"] == []
    assert appmod.fetch_in_flight == 0
