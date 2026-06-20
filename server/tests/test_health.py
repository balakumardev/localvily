import httpx
from httpx import ASGITransport

import browser_relay.app as appmod
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
    appmod.last_poll_time = 0.0  # isolate from any prior /pending poll in the suite
    async with await _client() as client:
        resp = await client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["extension_connected"] is False


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
