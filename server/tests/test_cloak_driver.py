import os
from pathlib import Path

import pytest

from browser_relay.drivers.cloak import CloakDriver, _shared_js_dir


def test_shared_js_dir_resolves_to_packaged_flat_files():
    d = _shared_js_dir()
    assert (d / "serp.js").exists(), f"serp.js not found under {d}"
    assert (d / "extract.js").exists()
    assert (d / "Readability.js").exists()


def test_driver_status_before_start_reports_unavailable():
    drv = CloakDriver()
    st = drv.status()
    assert st["available"] is False
    assert "profile_path" in st


def _chromium_available() -> bool:
    # patchright/playwright stores browsers under ms-playwright cache
    try:
        from patchright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False
    return os.environ.get("BROWSER_RELAY_RUN_CLOAK_TESTS") == "1"


@pytest.mark.skipif(not _chromium_available(),
                    reason="set BROWSER_RELAY_RUN_CLOAK_TESTS=1 with chromium installed to run live cloak tests")
async def test_cloak_fetch_real_page_returns_text():
    drv = CloakDriver()
    await drv.start()
    try:
        result = await drv.fetch("https://en.wikipedia.org/wiki/Consistent_hashing", include_html=False)
        assert result["status"] == "ok"
        assert result["driver"] == "cloak"
        assert result["length"] >= 1000
    finally:
        await drv.close()


@pytest.mark.skipif(not _chromium_available(),
                    reason="set BROWSER_RELAY_RUN_CLOAK_TESTS=1 with chromium installed to run live cloak tests")
async def test_cloak_search_real_returns_results():
    drv = CloakDriver()
    await drv.start()
    try:
        result = await drv.search("consistent hashing", k=10, engine="bing")
        assert result["status"] in ("ok", "action_required")
        if result["status"] == "ok":
            assert result["count"] >= 1
            assert result["driver"] == "cloak"
    finally:
        await drv.close()
