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


async def test_request_never_raises_on_transport_error(monkeypatch):
    # _request must always return a dict, even when the underlying client raises
    # something other than ConnectError/ReadTimeout. AUTO_MANAGE_SERVER is off here,
    # so no respawn is attempted.
    monkeypatch.setattr(mcpserver, "AUTO_MANAGE_SERVER", False)

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(mcpserver, "_client", lambda: _BoomClient())
    result = await mcpserver._request("GET", "/health")
    assert result["status"] == "error"
    # ConnectTimeout subclasses httpx.TimeoutException, so it maps to the timeout
    # message. The point: the old ConnectError/ReadTimeout-only handler would have
    # let this escape; now it always returns an error dict instead of raising.
    assert "Timed out" in result["error"]


async def test_request_connect_error_maps_to_cannot_connect(monkeypatch):
    monkeypatch.setattr(mcpserver, "AUTO_MANAGE_SERVER", False)

    class _ConnClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(mcpserver, "_client", lambda: _ConnClient())
    result = await mcpserver._request("GET", "/health")
    assert result["status"] == "error"
    assert "Cannot connect" in result["error"]


async def test_request_handles_non_json_200(monkeypatch):
    class _HtmlClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            return httpx.Response(200, text="<html>not json</html>")

    monkeypatch.setattr(mcpserver, "_client", lambda: _HtmlClient())
    result = await mcpserver._request("GET", "/health")
    assert result["status"] == "error"
    assert "non-JSON" in result["error"]
