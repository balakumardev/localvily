import asyncio

from tests.conftest import drive_extension


async def test_search_roundtrip(client):
    def respond(job):
        assert job["kind"] == "search"
        assert job["query"] == "consistent hashing"
        return {"results": [{"title": "T", "url": "https://e.com", "snippet": "s"}]}

    caller = client.get("/search", params={"q": "consistent hashing", "k": 10})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] == "relay"
    assert body["count"] == 1
    assert body["results"][0]["url"] == "https://e.com"


async def test_fetch_roundtrip(client):
    def respond(job):
        assert job["kind"] == "fetch"
        return {"title": "Doc", "text": "x" * 1500, "excerpt": "ex", "length": 1500}

    caller = client.get("/fetch", params={"url": "https://e.com/a"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "ok"
    assert body["length"] == 1500
    assert body["title"] == "Doc"


async def test_search_error_payload_marks_status_error(client):
    def respond(job):
        return {"error": "blocked"}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "blocked"
