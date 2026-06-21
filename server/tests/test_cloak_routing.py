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
