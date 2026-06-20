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
