import browser_relay.app as appmod


class FakeCloakDriver:
    def __init__(self):
        self.available = True
        self.searched = []

    def status(self):
        return {"available": True, "profile_path": "/tmp/x", "pages_open": 0}

    async def start(self):
        self.available = True

    async def search(self, query, k=10, engine="bing"):
        self.searched.append(query)
        if query == "BLOCKME":
            return {"status": "action_required", "query": query, "engine": engine,
                    "driver": "cloak", "action": "solve_captcha", "_page": object()}
        return {"status": "ok", "query": query, "engine": engine, "driver": "cloak",
                "count": 1, "results": [{"title": "T", "url": "https://e", "snippet": "s"}]}

    async def fetch(self, url, include_html=False):
        return {"status": "ok", "url": url, "driver": "cloak",
                "title": "D", "text": "y" * 1200, "excerpt": "e", "length": 1200}


def setup_function():
    appmod.actions.clear()
    appmod.cloak_pages.clear()


async def test_cloak_search_routes_to_driver(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    resp = await client.get("/search", params={"q": "hello", "driver": "cloak"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] == "cloak"
    assert body["count"] == 1
    assert fake.searched == ["hello"]


async def test_cloak_fetch_routes_to_driver(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    resp = await client.get("/fetch", params={"url": "https://x", "driver": "cloak"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] == "cloak"
    assert body["length"] == 1200


async def test_cloak_block_registers_cloak_action_with_page(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    resp = await client.get("/search", params={"q": "BLOCKME", "driver": "cloak"})
    body = resp.json()
    assert body["status"] == "action_required"
    assert body["driver"] == "cloak"
    token = body["resume_token"]
    action = appmod.actions[token]
    assert action.driver == "cloak"
    # the page is held in the cloak_pages registry under the action's tab_id
    assert action.tab_id in appmod.cloak_pages
    # the action_required payload must NOT leak the raw _page object
    assert "_page" not in body


async def test_health_has_drivers_substructure(client, monkeypatch):
    fake = FakeCloakDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    body = (await client.get("/health")).json()
    assert "drivers" in body
    assert "relay" in body["drivers"]
    assert "cloak" in body["drivers"]
    assert body["drivers"]["cloak"]["available"] is True
    # backward-compat: top-level relay fields still present
    assert "extension_connected" in body


class ReCheckCloakDriver(FakeCloakDriver):
    def __init__(self, recheck_results):
        super().__init__()
        self._recheck_results = list(recheck_results)

    async def recheck(self, page, kind, payload):
        r = self._recheck_results.pop(0)
        return r


async def test_cloak_resume_clears_and_resolves(client, monkeypatch):
    # First a block, then resume → recheck returns ok.
    fake = ReCheckCloakDriver([
        {"status": "ok", "url": "https://x", "driver": "cloak",
         "title": "D", "text": "z" * 1100, "length": 1100},
    ])
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)

    # Block first (fetch login).
    monkeypatch.setattr(fake, "fetch", _blocking_fetch)
    resp = await client.get("/fetch", params={"url": "https://x", "driver": "cloak"})
    token = resp.json()["resume_token"]
    assert resp.json()["status"] == "action_required"
    assert appmod.actions[token].driver == "cloak"

    # Resume → recheck ok → resolved + page handle freed.
    rresp = await client.post(f"/resume/{token}")
    rbody = rresp.json()
    assert rbody["status"] == "ok"
    assert rbody["driver"] == "cloak"
    assert token not in appmod.actions
    again = (await client.post(f"/resume/{token}")).json()
    assert again["status"] == "error"


async def _blocking_fetch(url, include_html=False):
    return {"status": "action_required", "url": url, "driver": "cloak",
            "action": "login", "_page": object()}


async def test_cloak_resume_still_blocked_keeps_token(client, monkeypatch):
    fake = ReCheckCloakDriver([
        {"status": "action_required", "url": "https://x", "driver": "cloak",
         "action": "login", "_page": object()},
        {"status": "ok", "url": "https://x", "driver": "cloak", "title": "D",
         "text": "z" * 1100, "length": 1100},
    ])
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)
    monkeypatch.setattr(fake, "fetch", _blocking_fetch)

    resp = await client.get("/fetch", params={"url": "https://x", "driver": "cloak"})
    token = resp.json()["resume_token"]

    r1 = (await client.post(f"/resume/{token}")).json()
    assert r1["status"] == "action_required"
    assert r1["resume_token"] == token        # same token kept
    assert token in appmod.actions
    assert len(appmod.actions) == 1

    r2 = (await client.post(f"/resume/{token}")).json()
    assert r2["status"] == "ok"
    assert token not in appmod.actions


class _ClosablePage:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_expired_cloak_action_closes_page_not_relay_close_tabs(client, monkeypatch):
    # When a cloak action expires via TTL, its held patchright page must be
    # closed + dropped from cloak_pages — NOT pushed onto the relay close_tabs
    # queue (whose ids are real Chrome tab ids). Regression test for the
    # cross-driver expiry bug.
    monkeypatch.setattr(appmod, "ACTION_TTL", 0.0)  # immediately expired
    page = _ClosablePage()

    class BlockingSearchDriver(FakeCloakDriver):
        async def search(self, query, k=10, engine="bing"):
            return {"status": "action_required", "query": query, "engine": engine,
                    "driver": "cloak", "action": "solve_captcha", "_page": page}

    fake = BlockingSearchDriver()
    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: fake)

    resp = await client.get("/search", params={"q": "x", "driver": "cloak"})
    token = resp.json()["resume_token"]
    handle = appmod.actions[token].tab_id
    assert handle in appmod.cloak_pages

    await appmod._sweep_expired_actions()

    assert token not in appmod.actions
    assert handle not in appmod.cloak_pages       # page handle freed
    assert page.closed is True                    # the patchright page was closed
    pend = (await client.get("/pending")).json()
    assert handle not in pend["close_tabs"]       # NOT misrouted to the extension
