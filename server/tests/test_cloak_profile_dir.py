import os
from pathlib import Path


def test_default_profile_dir_is_user_writable_cache_not_repo(monkeypatch):
    monkeypatch.delenv("BROWSER_RELAY_CLOAK_PROFILE_DIR", raising=False)
    import importlib
    import browser_relay.drivers.cloak as cloak
    importlib.reload(cloak)
    p = Path(cloak.CLOAK_PROFILE_DIR)
    # must NOT be inside the repo / site-packages of the source tree
    assert "site-packages" not in str(p)
    assert "browser_relay/drivers" not in str(p)
    # must be under a user cache location
    assert "browser-relay" in str(p)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("BROWSER_RELAY_CLOAK_PROFILE_DIR", "/tmp/custom-cloak")
    import importlib
    import browser_relay.drivers.cloak as cloak
    importlib.reload(cloak)
    assert cloak.CLOAK_PROFILE_DIR == "/tmp/custom-cloak"
    monkeypatch.delenv("BROWSER_RELAY_CLOAK_PROFILE_DIR", raising=False)
    importlib.reload(cloak)
