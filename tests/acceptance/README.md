# Live Acceptance — browser-relay-mcp Phase 2

`run_acceptance.py` drives the running relay's HTTP endpoints and asserts the
PRD acceptance criteria against a live Chrome + extension. It is **human-attended**:
criteria C1–C4 require a real browser with the unpacked extension loaded and
signed in, so it cannot be run fully headlessly.

## Prerequisites

1. **Relay running** — from the repo root:
   ```bash
   cd server && uv run browser-relay-mcp --backend --port 15552
   ```
2. **Chrome + unpacked extension** — open Chrome, sign in, then load the unpacked
   extension at `extension/` (chrome://extensions → Developer mode → Load unpacked).
   In the extension's popup/options, set the **server URL** to `http://localhost:15552`.
   The popup status should read **connected**.

## Run

```bash
uv run --with httpx python tests/acceptance/run_acceptance.py
```

Expected output ends with: `ALL ACCEPTANCE CRITERIA PASSED (C1, C2, C3, C4)`.

## What each criterion checks

| Criterion | Check |
|-----------|-------|
| **C4** | `/health` reports `extension_connected: true`. If not, the script fails loud immediately ("open Chrome with the extension loaded") — every other criterion depends on a live extension, so it short-circuits here. |
| **C1** | A single `/search?q=consistent hashing&k=10` returns `status=ok` with **≥ 8 results**. |
| **C2** | `/fetch` of a JS-heavy article (`en.wikipedia.org/wiki/Consistent_hashing`) returns `status=ok` with **≥ 1000 chars** of extracted text. |
| **C3** | **The headline test.** 50 sequential `/search` calls must produce **0 errors and 0 silent-empty** result sets. Any empties/errors here are the failure the whole project targets — investigate Bing block detection vs. real request cadence before declaring Phase 2 done. |

## Criterion 5 (STORM adapter)

C5 is covered by the adapter unit tests, not this live script:

- `adapters/tests/test_storm.py` exercises the STORM adapter (`to_storm`).
- `search_and_fetch` (relay MCP tool) feeds `to_storm` — the search+fetch results
  shape is what the STORM adapter consumes.

Run it with:
```bash
cd adapters && python -m pytest tests/test_storm.py
```
