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
    appmod.actions.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


async def test_resume_tool_passthrough(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    drive_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    def block_then_ok(job):
        if "recheck_tab_id" in job:
            return {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}
        return {"action_required": True, "action": "solve_captcha", "tab_id": 11}

    async with drive_client:
        search_caller = mcpserver.search("q")
        _, sout = await asyncio.gather(drive_extension(drive_client, block_then_ok), search_caller)
        token = json.loads(sout)["resume_token"]

        resume_caller = mcpserver.resume(token)
        _, rout = await asyncio.gather(drive_extension(drive_client, block_then_ok), resume_caller)
    body = json.loads(rout)
    assert body["status"] == "ok"
    assert body["count"] == 1


async def test_resume_tool_unknown_token(monkeypatch):
    def factory():
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=5.0)
    monkeypatch.setattr(mcpserver, "_client", factory)
    out = await mcpserver.resume("bogus")
    body = json.loads(out)
    assert body["status"] == "error"
