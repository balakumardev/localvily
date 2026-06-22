# browser-relay-mcp — Plan 5: Packaging Parity + Final Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make browser-relay-mcp installable and distributable (uvx/PyPI + load-unpacked Chrome extension + Web Store assets), fix the confirmed packaging defects from the Phase 4 review (shared JS must ship in the wheel; cloak profile dir must be user-writable; secrets must be gitignored), and clear the accumulated minor polish items.

**Architecture:** Vendor the three shared JS files into the `browser_relay` package so the cloak driver finds them under any install layout; relocate the cloak profile to a user cache dir; finish the popup/options driver display; write README + privacy-policy; package the Chrome extension zip + Web Store assets; tidy the small inherited nits.

**Tech Stack:** hatchling packaging (force-include), Python stdlib for paths, the existing extension/relay/MCP code.

## Global Constraints

- **Shared JS must ship in the wheel.** The cloak driver currently resolves shared JS via `Path(__file__).parents[3] / "extension"` — works in the repo, silently breaks on install (extension/ is outside the package). The 3 files (`inject/serp.js`, `inject/extract.js`, `lib/Readability.js`) must be packaged inside `browser_relay/` and resolved from there, with the `BROWSER_RELAY_SHARED_JS_DIR` env override preserved.
- **`CLOAK_PROFILE_DIR` default must be user-writable** (a cache dir under the user's home, NOT a repo/site-packages-relative path). Env override `BROWSER_RELAY_CLOAK_PROFILE_DIR` preserved.
- **Secrets gitignored:** `.cloak-profile/` (legacy default) AND the new cache profile must never be committed (the cache one is outside the repo, so just gitignore the legacy `.cloak-profile/`).
- **No regression:** all 41 tests stay green; the 4 live cloak tests still pass; the relay path unchanged. The shared-JS relocation must keep BOTH the extension (which loads from `extension/`) AND the cloak driver (which now loads from the packaged copy) working — they share ONE source, copied at build/sync time.
- **Single source of truth preserved:** the canonical shared JS lives in `extension/` (where `build-inject.mjs` generates it and the extension loads it). The packaged copy under `browser_relay/` is a *build artifact* synced from `extension/`, never hand-edited — guarded by a sync check (mirrors the Phase 2 inject drift test).
- **Don't break the extension:** the extension keeps loading `inject/serp.js` etc. from `extension/` (its manifest paths are relative to `extension/`). Only the cloak driver's copy moves into the package.
- **Version stays `0.1.0`** unless a task says otherwise.

---

### Task 1: Ship shared JS inside the package + resolve from there (fixes cloak-breaks-on-install)

**Files:**
- Create: `server/browser_relay/shared_js/serp.js`, `extract.js`, `Readability.js` (synced copies)
- Create: `server/sync_shared_js.py` (copies the 3 canonical files from `extension/` into the package)
- Modify: `server/browser_relay/drivers/cloak.py` (`_shared_js_dir` resolves to the packaged dir)
- Modify: `server/pyproject.toml` (force-include the shared_js files in the wheel)
- Create: `server/tests/test_shared_js_packaging.py`

**Interfaces:**
- Consumes: canonical `extension/inject/serp.js`, `extension/inject/extract.js`, `extension/lib/Readability.js`.
- Produces:
  - `browser_relay/shared_js/{serp.js,extract.js,Readability.js}` — packaged copies.
  - `_shared_js_dir()` returns the packaged dir by default; the driver loads `serp.js`/`Readability.js`/`extract.js` from there (note: flat filenames, not the `inject/`+`lib/` subpaths). Env override preserved.
  - `sync_shared_js.py`: idempotent copy script.
  - A test asserting the packaged copies are byte-identical to the canonical extension sources (drift guard).

- [ ] **Step 1: Write the sync script**

`server/sync_shared_js.py`:
```python
"""Copy the canonical shared JS from extension/ into the browser_relay package
so it ships in the wheel and the cloak driver finds it under any install layout.

Run from the repo after changing extension/inject/*.js or lib/Readability.js
(or after `npm run build:inject`):  python server/sync_shared_js.py
"""
from pathlib import Path
import shutil

REPO = Path(__file__).resolve().parents[1]
SRC = {
    "serp.js": REPO / "extension" / "inject" / "serp.js",
    "extract.js": REPO / "extension" / "inject" / "extract.js",
    "Readability.js": REPO / "extension" / "lib" / "Readability.js",
}
DEST = REPO / "server" / "browser_relay" / "shared_js"


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    for name, src in SRC.items():
        if not src.exists():
            raise SystemExit(f"canonical shared JS missing: {src}")
        shutil.cop2 if False else shutil.copyfile(src, DEST / name)
    print(f"synced {len(SRC)} shared JS files into {DEST}")


if __name__ == "__main__":
    main()
```
(Note: use `shutil.copyfile(src, DEST / name)` — the `copy2 if False else` is a typo; write the clean line below.)

Clean version of the loop body:
```python
    for name, src in SRC.items():
        if not src.exists():
            raise SystemExit(f"canonical shared JS missing: {src}")
        shutil.copyfile(src, DEST / name)
```

- [ ] **Step 2: Run the sync to create the packaged copies**

Run:
```bash
cd /Users/bkumara/personal/localvily && npm run build:inject && python server/sync_shared_js.py && ls server/browser_relay/shared_js/
```
Expected: `serp.js extract.js Readability.js` present. Verify no `export` in serp/extract (`grep -c export server/browser_relay/shared_js/serp.js` → 0) and Readability is ~90KB.

- [ ] **Step 3: Write the failing drift test**

`server/tests/test_shared_js_packaging.py`:
```python
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
```

- [ ] **Step 4: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_shared_js_packaging.py -v`
Expected: `test_cloak_driver_resolves_packaged_shared_js` FAILS — `_shared_js_dir` still points at `extension/` with `inject/`+`lib/` subpaths, not the flat packaged dir.

- [ ] **Step 5: Update `_shared_js_dir` + the driver's file map**

In `server/browser_relay/drivers/cloak.py`, replace `_shared_js_dir`:
```python
def _shared_js_dir() -> Path:
    override = os.environ.get("BROWSER_RELAY_SHARED_JS_DIR")
    if override:
        return Path(override)
    # Packaged copy shipped inside browser_relay (works under any install layout).
    return Path(__file__).resolve().parent.parent / "shared_js"
```
And update the `__init__` file map to the flat packaged filenames:
```python
        for key, rel in (("serp", "serp.js"), ("readability", "Readability.js"),
                         ("extract", "extract.js")):
            p = d / rel
```

- [ ] **Step 6: Force-include the shared JS in the wheel**

In `server/pyproject.toml`, under the wheel target, add:
```toml
[tool.hatch.build.targets.wheel]
packages = ["browser_relay"]

[tool.hatch.build.targets.wheel.force-include]
"browser_relay/shared_js/serp.js" = "browser_relay/shared_js/serp.js"
"browser_relay/shared_js/extract.js" = "browser_relay/shared_js/extract.js"
"browser_relay/shared_js/Readability.js" = "browser_relay/shared_js/Readability.js"
```
(Since `shared_js/` is inside the `browser_relay` package dir, hatchling includes `.js` data files only if told — force-include guarantees it.)

- [ ] **Step 7: Run to verify pass + full suite + live cloak**

Run:
```bash
cd server && uv run pytest tests/test_shared_js_packaging.py -v && uv run pytest -q
BROWSER_RELAY_RUN_CLOAK_TESTS=1 uv run pytest tests/test_cloak_driver.py -q
```
Expected: packaging tests pass; full suite green; the 4 live cloak tests still pass (driver now loads JS from the packaged dir — the live fetch/search still work).

- [ ] **Step 8: Verify the wheel actually contains the JS**

Run:
```bash
cd server && uv build --wheel 2>&1 | tail -2 && python -c "import zipfile,glob; w=sorted(glob.glob('dist/*.whl'))[-1]; names=zipfile.ZipFile(w).namelist(); print([n for n in names if 'shared_js' in n])"
```
Expected: the list shows all three `browser_relay/shared_js/*.js` entries. (Clean up `dist/` after; add `dist/` and `build/` to .gitignore in Task 3.)

- [ ] **Step 9: Commit**

```bash
git add server/sync_shared_js.py server/browser_relay/shared_js/ server/browser_relay/drivers/cloak.py server/pyproject.toml server/tests/test_shared_js_packaging.py
git commit -m "fix(cloak): ship shared JS inside the wheel + resolve from package (cloak works on install)"
```

---

### Task 2: User-writable cloak profile dir default

**Files:**
- Modify: `server/browser_relay/drivers/cloak.py`
- Create: `server/tests/test_cloak_profile_dir.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `CLOAK_PROFILE_DIR` defaults to a per-user cache dir (`~/Library/Caches/browser-relay/cloak-profile` on macOS, `%LOCALAPPDATA%\browser-relay\cloak-profile` on Windows, `$XDG_CACHE_HOME/browser-relay/cloak-profile` else), env `BROWSER_RELAY_CLOAK_PROFILE_DIR` override preserved. Reuse the same OS-cache logic the MCP server already uses (`_state_dir` in `mcp_server/server.py`).

- [ ] **Step 1: Write the failing test**

`server/tests/test_cloak_profile_dir.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_cloak_profile_dir.py -v`
Expected: FAIL — default is currently `<repo>/.cloak-profile` (contains `browser_relay/drivers` ancestry or the repo path), not a user cache dir.

- [ ] **Step 3: Implement the user-cache default**

In `cloak.py`, replace the `CLOAK_PROFILE_DIR` definition:
```python
def _user_cache_dir() -> Path:
    import sys
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "browser-relay"


CLOAK_PROFILE_DIR = os.environ.get(
    "BROWSER_RELAY_CLOAK_PROFILE_DIR",
    str(_user_cache_dir() / "cloak-profile"),
)
```

- [ ] **Step 4: Run to verify pass + full suite**

Run: `cd server && uv run pytest tests/test_cloak_profile_dir.py -v && uv run pytest -q`
Expected: pass; full suite green.

- [ ] **Step 5: Commit**

```bash
git add server/browser_relay/drivers/cloak.py server/tests/test_cloak_profile_dir.py
git commit -m "fix(cloak): default profile dir to user cache (not repo/site-packages)"
```

---

### Task 3: Polish — engine validation, cloak search throttle, recheck html parity, acceptance C6 label, gitignore

**Files:**
- Modify: `server/browser_relay/drivers/cloak.py`
- Modify: `tests/acceptance/run_acceptance.py`
- Modify: `.gitignore`
- Modify: `server/tests/test_cloak_routing.py` (or `test_cloak_driver.py`) for the new guards

**Interfaces:**
- Consumes: existing cloak driver.
- Produces: cloak `search` bounded by a search semaphore (concurrency 1) to mirror relay's anti-CAPTCHA caution; `engine` other than `"bing"` normalized to bing (only bing is supported) with the result echoing the actual engine used; `recheck` honors `include_html`; acceptance success-print includes C6 when it ran; `.gitignore` covers `.cloak-profile/`, `dist/`, `build/`.

- [ ] **Step 1: Write failing tests for the guards**

Append to `server/tests/test_cloak_driver.py`:
```python
def test_serp_url_only_targets_bing():
    from browser_relay.drivers.cloak import _serp_url
    u = _serp_url("consistent hashing", 10)
    assert u.startswith("https://www.bing.com/search?")


def test_driver_has_search_semaphore():
    drv = CloakDriver()
    # cloak search must be throttled like relay (anti-CAPTCHA), not unbounded
    assert hasattr(drv, "_search_sem")
    assert drv._search_sem._value == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd server && uv run pytest tests/test_cloak_driver.py -k "semaphore or bing" -v`
Expected: FAIL — `_search_sem` doesn't exist.

- [ ] **Step 3: Add the search semaphore + engine normalization + recheck html**

In `cloak.py` `__init__`, add:
```python
        self._search_sem = asyncio.Semaphore(1)  # serialize cloak searches (anti-CAPTCHA, mirrors relay)
```
Wrap the body of `search()` in `async with self._search_sem:` (around the existing try). Normalize engine — at the top of `search()`:
```python
        engine = "bing"  # only Bing is supported by the cloak SERP path today
```
(so the echoed `engine` reflects what was actually used). In `recheck()`, for the fetch branch, honor `include_html` from payload: after computing `content`, if `payload.get("include_html")`, add `content = {**content, "html": await page.content()}` BEFORE closing the page, and include `"html"` in the key set.

- [ ] **Step 4: Fix acceptance C6 label**

In `tests/acceptance/run_acceptance.py`, track whether C6 ran and include it in the final success message. Replace the final success print:
```python
    ran = ["C1", "C2", "C3", "C4"]
    if os.environ.get("BROWSER_RELAY_ACCEPT_CLOAK") == "1":
        ran.append("C6")
    print(f"\nALL ACCEPTANCE CRITERIA PASSED ({', '.join(ran)})")
```
(Move `import os` to the top of the file with the other imports.)

- [ ] **Step 5: Update .gitignore**

Append to `.gitignore`:
```
# Browser relay
.cloak-profile/
dist/
build/
```

- [ ] **Step 6: Run to verify pass + full suite**

Run: `cd server && uv run pytest tests/test_cloak_driver.py -v && uv run pytest -q`
Expected: pass; full suite green. Confirm `.cloak-profile/` is now ignored: `git check-ignore .cloak-profile/` → prints the path.

- [ ] **Step 7: Commit**

```bash
git add server/browser_relay/drivers/cloak.py tests/acceptance/run_acceptance.py .gitignore server/tests/test_cloak_driver.py
git commit -m "polish(cloak): serialize searches, normalize engine to bing, recheck html parity, C6 label, gitignore secrets"
```

---

### Task 4: Popup shows both drivers; options unchanged

**Files:**
- Modify: `extension/popup.js`
- Modify: `extension/popup.css` (if needed for the extra rows)

**Interfaces:**
- Consumes: `/health` (now has `drivers:{relay,cloak}`).
- Produces: the popup shows relay connectivity AND cloak availability from `health.drivers`, falling back gracefully if `drivers` is absent (older relay).

- [ ] **Step 1: Update the popup to read `drivers`**

In `extension/popup.js`, in the `refresh()` success branch, extend the detail to show cloak:
```javascript
    const drivers = h.drivers || {};
    const cloak = drivers.cloak || {};
    detail.innerHTML = `
      <dt>extension</dt><dd>${h.extension_connected ? "connected" : h.extension_status}</dd>
      <dt>cloak</dt><dd>${cloak.available ? "ready" : "unavailable"}</dd>
      <dt>in flight</dt><dd>${h.in_flight}</dd>
      <dt>queued</dt><dd>${h.search_queued + h.fetch_queued}</dd>`;
```
(Values are still trusted enums/ints/booleans from the loopback relay — innerHTML stays safe.)

- [ ] **Step 2: Sanity check**

Run: `cd /Users/bkumara/personal/localvily && node --check extension/popup.js && echo OK`
Expected: `OK`. (Full visual check happens during the live acceptance run.)

- [ ] **Step 3: Commit**

```bash
git add extension/popup.js extension/popup.css
git commit -m "feat(ext): popup shows cloak driver availability from health.drivers"
```

---

### Task 5: README + privacy-policy + extension zip

**Files:**
- Create: `README.md` (repo root)
- Create: `docs/privacy-policy.md`
- Create: `scripts/package-extension.sh` (zips `extension/` for the Web Store / manual install)

**Interfaces:**
- Consumes: the whole project.
- Produces: install + usage docs, a privacy policy (extension stores), a reproducible extension-zip script.

- [ ] **Step 1: Write the README**

`README.md` — cover: what it is (unlimited session-authenticated web search+fetch via the user's browser, behind an MCP server); architecture diagram (MCP → relay :15552 → relay driver via Chrome extension OR cloak driver via embedded patchright); install (`uvx browser-relay-mcp`; load the unpacked `extension/` in Chrome and set server URL to `http://localhost:15552`; for cloak: `patchright install chromium` once); the MCP config block; the four tools (`search`/`fetch`/`search_and_fetch`/`resume`/`health`) with the `driver` param; the escalation flow (action_required → user solves → resume); the STORM adapter (`adapters/storm.py`); and a "Run the acceptance checks" section. Include the exact MCP config:
```json
{ "mcpServers": { "browser-relay": { "command": "uvx", "args": ["browser-relay-mcp"] } } }
```

- [ ] **Step 2: Write the privacy policy**

`docs/privacy-policy.md` — the extension reads page content only for pages the relay explicitly drives (search result pages + fetched URLs), sends results only to the local relay on `localhost:15552`, stores only the server URL in `chrome.storage.local`, no third-party transmission, no analytics. Mirror the structure of a typical minimal extension privacy policy.

- [ ] **Step 3: Write the extension-zip script**

`scripts/package-extension.sh`:
```bash
#!/usr/bin/env bash
# Package the Chrome extension into a zip for the Web Store / manual install.
set -euo pipefail
cd "$(dirname "$0")/.."
npm run build:inject          # ensure generated inject bundles are current
OUT="dist/browser-relay-extension.zip"
mkdir -p dist
rm -f "$OUT"
( cd extension && zip -r "../$OUT" . -x '*/tests/*' 'tests/*' )
echo "wrote $OUT"
```
Make it executable: `chmod +x scripts/package-extension.sh`.

- [ ] **Step 4: Verify the zip builds**

Run: `cd /Users/bkumara/personal/localvily && bash scripts/package-extension.sh && unzip -l dist/browser-relay-extension.zip | grep -E "manifest.json|background.js|inject/serp.js" `
Expected: the zip lists manifest, background, and the inject bundles. (`dist/` is gitignored from Task 3, so the zip isn't committed.)

- [ ] **Step 5: Commit**

```bash
git add README.md docs/privacy-policy.md scripts/package-extension.sh
git commit -m "docs: README, privacy policy, extension packaging script"
```

---

## Self-Review

**1. Spec coverage (spec §16 distribution/packaging + Phase 4 carry-forward):**
- Shared JS in wheel + resolved from package (THE critical fix) → Task 1. ✓
- patchright dep already declared (Phase 4); README documents `patchright install chromium` → Task 5. ✓
- `.cloak-profile/` + dist/build gitignored → Task 3. ✓
- `CLOAK_PROFILE_DIR` user-writable default → Task 2. ✓
- uvx/PyPI entry (already exists from Phase 1) + README config → Task 5. ✓
- Web Store assets/zip → Task 5 (zip script; full PNG art is optional/manual). ✓
- Popup driver display → Task 4. ✓
- Minor polish (engine normalize, cloak search throttle, recheck html, C6 label) → Task 3. ✓

**2. Placeholder scan:** The sync-script Step 1 flags its own typo and gives the clean line — the implementer writes the clean version. README/privacy content is described by required sections (standard docs, not code) — acceptable for prose deliverables. No code-step placeholders.

**3. Type consistency:** `_shared_js_dir()` (package-relative), `_user_cache_dir()`, `_serp_url`, `_search_sem`, `CLOAK_PROFILE_DIR` consistent. The packaged shared-JS filenames are flat (`serp.js`/`extract.js`/`Readability.js`) — Task 1 updates BOTH `_shared_js_dir` and the `__init__` file map together, and the drift test + live cloak test verify the driver still loads them.

**4. Single-source integrity:** canonical JS stays in `extension/`; the packaged copy is synced (`sync_shared_js.py`) and drift-guarded by a test, so it can't silently diverge — same discipline as the Phase 2 inject build.

---

## Final-merge checklist (after Task 5)
- Run the full server suite (expect ~50 passed + 2 skipped) and the extension unit suite (`npm test`).
- Run the live cloak tests once (`BROWSER_RELAY_RUN_CLOAK_TESTS=1`) — confirm still green after the JS relocation.
- The remaining deferred item is the **live attended-Chrome acceptance run** (C1–C3 with the extension loaded) — environmental, surfaced to the user.
- Then: whole-branch final review across ALL phases, then superpowers:finishing-a-development-branch.
