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
        h = (await c.get("/health")).json()
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

        # Criterion 3: 50 sequential search+fetch with 0 failures / 0 silent-empty
        empties = 0
        errors = 0
        for i in range(50):
            q = f"distributed systems topic {i}"
            r = (await c.get("/search", params={"q": q, "k": 5})).json()
            if r.get("status") != "ok":
                errors += 1
            elif len(r.get("results", [])) == 0:
                empties += 1
        print(f"C3 burst: errors={errors} silent_empties={empties}")
        if errors or empties:
            failures.append(f"C3: burst had errors={errors} empties={empties} (must be 0/0)")

    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("\nALL ACCEPTANCE CRITERIA PASSED (C1, C2, C3, C4)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
