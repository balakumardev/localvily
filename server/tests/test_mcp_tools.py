import asyncio
import json

import httpx
from httpx import ASGITransport

import browser_relay.app as appmod
from browser_relay.app import app
from browser_relay.mcp_server import server as mcpserver
from tests.conftest import drive_extension


def setup_function():
    appmod.search_queue.clear()
    appmod.fetch_queue.clear()
    appmod.jobs.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


def _patch_client(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_search_tool_passthrough(monkeypatch):
    drive_client = _patch_client(monkeypatch)

    def respond(job):
        return {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}

    async with drive_client:
        caller = mcpserver.search("consistent hashing", k=10)
        _, out = await asyncio.gather(drive_extension(drive_client, respond), caller)
    body = json.loads(out)
    assert body["status"] == "ok"
    assert body["count"] == 1


async def test_cloak_driver_rejected(monkeypatch):
    _patch_client(monkeypatch)
    out = await mcpserver.search("x", driver="cloak")
    body = json.loads(out)
    assert body["status"] == "error"
    assert "cloak" in body["error"]
