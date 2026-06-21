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
    action = appmod.actions[body["resume_token"]]
    assert action.tab_id == 99
    assert action.kind == "fetch"
    assert action.action == "login"


async def test_resume_unknown_token_errors(client):
    resp = await client.post("/resume/nope-not-real")
    body = resp.json()
    assert body["status"] == "error"
    assert "expired" in body["error"] or "unknown" in body["error"]


async def test_resume_rechecks_tab_and_returns_ok(client):
    # First: a search escalates (block), registering an action with tab 7.
    def block_then_succeed(job):
        if "recheck_tab_id" in job:
            # recheck job — challenge now cleared, return real results
            return {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}
        return {"action_required": True, "action": "solve_captcha", "tab_id": 7}

    caller = client.get("/search", params={"q": "blocked-then-ok"})
    _, resp = await asyncio.gather(drive_extension(client, block_then_succeed), caller)
    token = resp.json()["resume_token"]
    assert resp.json()["status"] == "action_required"

    # Then: resume → recheck job served by the same simulated extension → ok results.
    resume_caller = client.post(f"/resume/{token}")
    _, rresp = await asyncio.gather(drive_extension(client, block_then_succeed), resume_caller)
    rbody = rresp.json()
    assert rbody["status"] == "ok"
    assert rbody["count"] == 1
    assert rbody["results"][0]["url"] == "https://e"
    # Resolved token can't be resumed again.
    again = (await client.post(f"/resume/{token}")).json()
    assert again["status"] == "error"


async def test_recheck_job_carries_tab_id_in_pending(client):
    def respond(job):
        return {"action_required": True, "action": "solve_captcha", "tab_id": 55}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    token = resp.json()["resume_token"]

    # Kick off a resume, then inspect what /pending hands the extension.
    resume_task = asyncio.create_task(client.post(f"/resume/{token}"))
    await asyncio.sleep(0.05)
    pend = (await client.get("/pending")).json()
    recheck_jobs = [j for j in pend["jobs"] if j.get("recheck_tab_id") == 55]
    assert len(recheck_jobs) == 1
    assert recheck_jobs[0]["kind"] == "search"
    resume_task.cancel()


async def test_expired_action_is_swept_and_tab_queued_for_close(client, monkeypatch):
    monkeypatch.setattr(appmod, "ACTION_TTL", 0.0)  # everything is immediately expired
    def respond(job):
        return {"action_required": True, "action": "solve_captcha", "tab_id": 321}

    caller = client.get("/search", params={"q": "x"})
    _, resp = await asyncio.gather(drive_extension(client, respond), caller)
    token = resp.json()["resume_token"]

    await appmod._sweep_expired_actions()  # directly invoke the sweep
    assert token not in appmod.actions
    # The expired action's tab is queued for the extension to close.
    pend = (await client.get("/pending")).json()
    assert 321 in pend["close_tabs"]


async def test_repeated_still_blocked_resume_keeps_one_stable_token(client):
    # The trickiest invariant: resuming while STILL blocked must keep exactly one
    # action record under the SAME original token (no orphan leak), across rounds,
    # until it finally clears. Serve each job EXACTLY ONCE here (as the real
    # extension does — one /result per /pending job), rather than via the
    # grace-polling drive_extension helper which would answer a job repeatedly.
    async def serve_one(body):
        for _ in range(200):
            jobs = (await client.get("/pending")).json()["jobs"]
            if jobs:
                await client.post(f"/result/{jobs[0]['job_id']}", json=body)
                return jobs[0]
            await asyncio.sleep(0.005)
        raise AssertionError("no job was dispatched to serve")

    blocked = {"action_required": True, "action": "solve_captcha", "tab_id": 8}
    ok = {"results": [{"title": "T", "url": "https://e", "snippet": "s"}]}

    # Initial search escalates.
    caller = asyncio.create_task(client.get("/search", params={"q": "x"}))
    await serve_one(blocked)
    resp = await caller
    token = resp.json()["resume_token"]
    assert resp.json()["status"] == "action_required"
    assert len(appmod.actions) == 1

    # First resume → still blocked: SAME token, still exactly one record.
    r1task = asyncio.create_task(client.post(f"/resume/{token}"))
    await serve_one(blocked)
    b1 = (await r1task).json()
    assert b1["status"] == "action_required"
    assert b1["resume_token"] == token
    assert len(appmod.actions) == 1
    assert token in appmod.actions

    # Second resume → still blocked again: invariant must hold across rounds.
    r2task = asyncio.create_task(client.post(f"/resume/{token}"))
    await serve_one(blocked)
    b2 = (await r2task).json()
    assert b2["status"] == "action_required"
    assert b2["resume_token"] == token
    assert len(appmod.actions) == 1

    # Third resume → cleared: token resolved + removed; a further resume errors.
    r3task = asyncio.create_task(client.post(f"/resume/{token}"))
    await serve_one(ok)
    b3 = (await r3task).json()
    assert b3["status"] == "ok"
    assert b3["count"] == 1
    assert token not in appmod.actions
    again = (await client.post(f"/resume/{token}")).json()
    assert again["status"] == "error"


async def test_action_carries_driver_field_default_relay(client):
    from browser_relay.app import Action
    a = Action("search", {"query": "q", "engine": "bing"}, 1, "solve_captcha")
    assert a.driver == "relay"
    b = Action("fetch", {"url": "u"}, 2, "login", driver="cloak")
    assert b.driver == "cloak"


async def test_action_required_payload_reflects_action_driver(client):
    # A cloak-registered action must surface driver:"cloak", not a hardcoded relay.
    import browser_relay.app as appmod
    from browser_relay.app import Action, _action_required_payload
    rec = Action("fetch", {"url": "https://x"}, 5, "login", driver="cloak")
    payload = _action_required_payload(rec)
    assert payload["driver"] == "cloak"
    assert payload["status"] == "action_required"
