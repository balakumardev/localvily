import httpx
from httpx import ASGITransport

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
    async with await _client() as client:
        resp = await client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["extension_connected"] is False
