"""Embedded stealth-browser driver (patchright). Self-contained — no external MCP.

Reuses the SAME in-page JS as the Chrome extension: extension/inject/serp.js
(globalThis.__serp) and extension/lib/Readability.js + extension/inject/extract.js
(globalThis.__extract, globalThis.__detectLogin), so parsing/extraction has one
source of truth across both drivers.
"""
import asyncio
import os
from pathlib import Path

CLOAK_PROFILE_DIR = os.environ.get(
    "BROWSER_RELAY_CLOAK_PROFILE_DIR",
    str(Path(__file__).resolve().parents[3] / ".cloak-profile"),
)
FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_RELAY_CLOAK_NAV_TIMEOUT_MS", "20000"))
SETTLE_MS = int(os.environ.get("BROWSER_RELAY_FETCH_SETTLE_MS", "800"))


def _shared_js_dir() -> Path:
    override = os.environ.get("BROWSER_RELAY_SHARED_JS_DIR")
    if override:
        return Path(override)
    # repo_root/server/browser_relay/drivers/cloak.py -> repo_root/extension
    return Path(__file__).resolve().parents[3] / "extension"


def _serp_url(query: str, k: int) -> str:
    from urllib.parse import quote_plus
    count = max(1, min(k, 50))
    return f"https://www.bing.com/search?q={quote_plus(query)}&count={count}"


class CloakDriver:
    def __init__(self):
        self._pw = None
        self._ctx = None
        self.available = False
        self._error = None
        self._search_page = None
        self._sem = asyncio.Semaphore(FETCH_CAP)
        self._js = {}
        d = _shared_js_dir()
        for key, rel in (("serp", "inject/serp.js"), ("readability", "lib/Readability.js"),
                         ("extract", "inject/extract.js")):
            p = d / rel
            self._js[key] = p.read_text(encoding="utf-8") if p.exists() else ""

    def status(self) -> dict:
        return {
            "available": self.available,
            "profile_path": CLOAK_PROFILE_DIR,
            "pages_open": 0 if not self._ctx else len(self._ctx.pages),
            **({"error": self._error} if self._error else {}),
        }

    async def start(self):
        if self.available:
            return
        try:
            from patchright.async_api import async_playwright
            self._pw = await async_playwright().start()
            Path(CLOAK_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
            launch_kwargs = dict(user_data_dir=CLOAK_PROFILE_DIR, headless=False)
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            self.available = True
            self._error = None
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self.available = False
            await self._cleanup()

    async def _cleanup(self):
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        self._search_page = None

    async def close(self):
        await self._cleanup()
        self.available = False

    async def _eval_serp(self, page, k: int) -> dict:
        await page.add_script_tag(content=self._js["serp"])
        return await page.evaluate(
            "(k) => globalThis.__serp.detectBlock(document) ? {blocked:true}"
            " : {results: globalThis.__serp.parse(document, k)}",
            k,
        )

    async def _eval_extract(self, page) -> dict:
        await page.add_script_tag(content=self._js["readability"])
        await page.add_script_tag(content=self._js["extract"])
        return await page.evaluate(
            "() => ({login: globalThis.__detectLogin(document), content: globalThis.__extract(document)})"
        )

    def _unavailable(self) -> dict:
        return {"status": "error", "driver": "cloak",
                "error": f"cloak browser unavailable: {self._error or 'not started'}"}

    async def search(self, query: str, k: int = 10, engine: str = "bing") -> dict:
        if not self.available:
            await self.start()
        if not self.available:
            return self._unavailable()
        base = {"query": query, "engine": engine, "driver": "cloak"}
        try:
            page = await self._ctx.new_page()
            await page.goto(_serp_url(query, k), timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(SETTLE_MS)
            res = await self._eval_serp(page, k)
            if res.get("blocked"):
                await page.bring_to_front()
                return {"status": "action_required", **base, "action": "solve_captcha",
                        "_page": page}  # _page handed to the relay's action registry
            await page.close()
            results = res.get("results", [])
            return {"status": "ok", **base, "count": len(results), "results": results}
        except Exception as exc:
            return {"status": "error", **base, "error": f"{type(exc).__name__}: {exc}"}

    async def fetch(self, url: str, include_html: bool = False) -> dict:
        if not self.available:
            await self.start()
        if not self.available:
            return self._unavailable()
        base = {"url": url, "driver": "cloak"}
        async with self._sem:
            try:
                page = await self._ctx.new_page()
                await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(SETTLE_MS)
                res = await self._eval_extract(page)
                if res.get("login"):
                    await page.bring_to_front()
                    return {"status": "action_required", **base, "action": "login", "_page": page}
                content = res.get("content") or {}
                if not content.get("text"):
                    await page.close()
                    return {"status": "error", **base, "error": "no extractable content"}
                if include_html:
                    content = {**content, "html": await page.content()}
                await page.close()
                return {"status": "ok", **base, **{k: content[k] for k in
                        ("title", "text", "excerpt", "length", "html") if k in content}}
            except Exception as exc:
                return {"status": "error", **base, "error": f"{type(exc).__name__}: {exc}"}

    async def recheck(self, page, kind: str, payload: dict) -> dict:
        """Re-evaluate a held page after the human acted (resume path)."""
        if kind == "search":
            base = {"query": payload.get("query"), "engine": payload.get("engine"), "driver": "cloak"}
            res = await self._eval_serp(page, payload.get("k", 10))
            if res.get("blocked"):
                await page.bring_to_front()
                return {"status": "action_required", **base, "action": "solve_captcha", "_page": page}
            await page.close()
            results = res.get("results", [])
            return {"status": "ok", **base, "count": len(results), "results": results}
        base = {"url": payload.get("url"), "driver": "cloak"}
        res = await self._eval_extract(page)
        if res.get("login"):
            await page.bring_to_front()
            return {"status": "action_required", **base, "action": "login", "_page": page}
        content = res.get("content") or {}
        if not content.get("text"):
            await page.close()
            return {"status": "error", **base, "error": "no extractable content"}
        await page.close()
        return {"status": "ok", **base, **{k: content[k] for k in
                ("title", "text", "excerpt", "length") if k in content}}


_driver: CloakDriver | None = None


def get_cloak_driver() -> CloakDriver:
    global _driver
    if _driver is None:
        _driver = CloakDriver()
    return _driver
