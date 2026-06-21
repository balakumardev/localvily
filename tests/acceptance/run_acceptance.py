"""Live acceptance checks for browser-relay-mcp Phase 2 (relay + Chrome extension).

Prerequisites:
  1. `cd server && uv run browser-relay-mcp --backend --port 15552` running.
  2. Chrome open, signed in, with the unpacked extension (extension/) loaded and its
     server URL set to http://localhost:15552.
Run: `uv run --with httpx python tests/acceptance/run_acceptance.py`
"""
import asyncio
import sys

import httpx

BASE = "http://localhost:15552"


async def main() -> int:
    failures = []
    async with httpx.AsyncClient(base_url=BASE, timeout=130.0) as c:
        # Criterion 4: health reports connected
        try:
            h = (await c.get("/health")).json()
        except httpx.ConnectError:
            print(f"FAIL relay not running at {BASE} — start it with "
                  "`cd server && uv run browser-relay-mcp --backend --port 15552`")
            return 1
        if not h.get("extension_connected"):
            failures.append("C4: extension not connected — open Chrome with the extension loaded")
            print("health:", h)
            for f in failures:
                print("FAIL", f)
            return 1
        print("C4 health connected:", h["extension_status"])

        # Criterion 1: search >= 8 results
        s = (await c.get("/search", params={"q": "consistent hashing", "k": 10})).json()
        n = len(s.get("results", []))
        print(f"C1 search returned {n} results, status={s.get('status')}")
        if s.get("status") != "ok" or n < 8:
            failures.append(f"C1: expected >=8 results, got {n} (status {s.get('status')})")

        # Criterion 2: fetch a JS-heavy article >= 1000 chars
        f2 = (await c.get("/fetch", params={"url": "https://en.wikipedia.org/wiki/Consistent_hashing"})).json()
        length = f2.get("length", 0)
        print(f"C2 fetch length={length}, status={f2.get('status')}")
        if f2.get("status") != "ok" or length < 1000:
            failures.append(f"C2: expected >=1000 chars, got {length}")

        # Criterion 3: 50 sequential search+fetch with 0 failures / 0 silent-empty.
        # Each iteration exercises BOTH legs — a search, then a fetch of its top
        # result — because fetch (the owned-background-tab path) is the more
        # failure-prone leg and is exactly what the headline criterion must test.
        search_errors = search_empties = fetch_errors = fetch_empties = 0
        for i in range(50):
            q = f"distributed systems topic {i}"
            r = (await c.get("/search", params={"q": q, "k": 5})).json()
            if r.get("status") != "ok":
                search_errors += 1
                continue
            results = r.get("results", [])
            if not results:
                search_empties += 1
                continue
            top_url = results[0]["url"]
            fr = (await c.get("/fetch", params={"url": top_url})).json()
            if fr.get("status") != "ok":
                fetch_errors += 1
            elif not fr.get("length"):
                fetch_empties += 1
        total_bad = search_errors + search_empties + fetch_errors + fetch_empties
        print(f"C3 burst: search_errors={search_errors} search_empties={search_empties} "
              f"fetch_errors={fetch_errors} fetch_empties={fetch_empties}")
        if total_bad:
            failures.append(
                f"C3: burst had search_errors={search_errors} search_empties={search_empties} "
                f"fetch_errors={fetch_errors} fetch_empties={fetch_empties} (must all be 0)")

    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("\nALL ACCEPTANCE CRITERIA PASSED (C1, C2, C3, C4)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
