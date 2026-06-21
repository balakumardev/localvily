import asyncio

import browser_relay.app as appmod
from tests.conftest import drive_extension


def setup_function():
    for d in (appmod.search_queue, appmod.fetch_queue):
        d.clear()
    appmod.jobs.clear()
    appmod.actions.clear()
    appmod.search_in_flight = 0
    appmod.fetch_in_flight = 0
    appmod.last_search_dispatch = 0.0


async def test_search_block_signal_becomes_action_required(client):
    def respond(job):
        # Simulate the extension detecting a CAPTCHA: it keeps the tab open and
        # posts an action_required signal instead of results.
        return {"action_required": True, "action": "solve_captcha", "tab_id": 4242}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "action_required"
    assert body["action"] == "solve_captcha"
    assert body["driver"] == "relay"
    assert body["resume_token"]  # non-empty token issued
    assert "message" in body
    # The action is registered, holding the tab id.
    action = appmod.actions[body["resume_token"]]
    assert action.tab_id == 4242
    assert action.kind == "search"


async def test_fetch_login_signal_becomes_action_required(client):
    def respond(job):
        return {"action_required": True, "action": "login", "tab_id": 99}

    caller = client.get("/fetch", params={"url": "https://paywalled.example/article"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    body = resp.json()
    assert body["status"] == "action_required"
    assert body["action"] == "login"
    assert body["url"] == "https://paywalled.example/article"
    assert body["resume_token"] in appmod.actions
