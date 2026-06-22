import browser_relay.app as appmod


async def test_lifespan_closes_cloak_driver(monkeypatch):
    closed = {"v": False}

    class FakeDrv:
        available = True
        async def close(self):
            closed["v"] = True
        def status(self):
            return {"available": True, "profile_path": "x", "pages_open": 0}

    monkeypatch.setattr(appmod, "get_cloak_driver", lambda: FakeDrv())
    # Exercise the lifespan context manager directly.
    async with appmod._lifespan(appmod.app):
        pass
    assert closed["v"] is True
