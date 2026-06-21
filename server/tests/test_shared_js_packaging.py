from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "server" / "browser_relay" / "shared_js"
CANON = {
    "serp.js": REPO / "extension" / "inject" / "serp.js",
    "extract.js": REPO / "extension" / "inject" / "extract.js",
    "Readability.js": REPO / "extension" / "lib" / "Readability.js",
}


def test_packaged_shared_js_matches_canonical_extension_source():
    for name, canon in CANON.items():
        pkg_file = PKG / name
        assert pkg_file.exists(), f"{pkg_file} missing — run python server/sync_shared_js.py"
        assert pkg_file.read_text(encoding="utf-8") == canon.read_text(encoding="utf-8"), (
            f"{name} drifted from {canon} — run python server/sync_shared_js.py")


def test_cloak_driver_resolves_packaged_shared_js():
    from browser_relay.drivers.cloak import _shared_js_dir
    d = _shared_js_dir()
    assert (d / "serp.js").exists()
    assert (d / "extract.js").exists()
    assert (d / "Readability.js").exists()
