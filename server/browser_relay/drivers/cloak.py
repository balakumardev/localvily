"""Embedded stealth-browser driver (patchright). Self-contained — no external MCP.

Reuses the SAME in-page JS as the Chrome extension: extension/inject/serp.js
(globalThis.__serp) and extension/lib/Readability.js + extension/inject/extract.js
(globalThis.__extract, globalThis.__detectLogin), so parsing/extraction has one
source of truth across both drivers.
"""
import asyncio
import os
from pathlib import Path

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
FETCH_CAP = int(os.environ.get("BROWSER_RELAY_FETCH_CAP", "5"))
NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_RELAY_CLOAK_NAV_TIMEOUT_MS", "20000"))
SETTLE_MS = int(os.environ.get("BROWSER_RELAY_FETCH_SETTLE_MS", "800"))


def _shared_js_dir() -> Path:
    override = os.environ.get("BROWSER_RELAY_SHARED_JS_DIR")
    if override:
        return Path(override)
    # Packaged copy shipped inside browser_relay (works under any install layout).
    return Path(__file__).resolve().parent.parent / "shared_js"


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
        self._sem = asyncio.Semaphore(FETCH_CAP)
        self._js = {}
        self._js_error = None
        d = _shared_js_dir()
        missing = []
        for key, rel in (("serp", "serp.js"), ("readability", "Readability.js"),
                         ("extract", "extract.js")):
            p = d / rel
            if p.exists():
                self._js[key] = p.read_text(encoding="utf-8")
            else:
                self._js[key] = ""
                missing.append(str(p))
        if missing:
            # Fail loud rather than silently eval("") (which leaves globalThis.__serp
            # undefined and breaks every cloak call at request time). Surfaced via
            # status()/start() so /health.drivers.cloak reports it.
            self._js_error = f"shared JS not found: {', '.join(missing)}"

    def status(self) -> dict:
        err = self._error or self._js_error
        return {
            "available": self.available,
            "profile_path": CLOAK_PROFILE_DIR,
            "pages_open": 0 if not self._ctx else len(self._ctx.pages),
            **({"error": err} if err else {}),
        }

    async def start(self):
        if self.available:
            return
        if self._js_error:
            # No point launching a browser we can't inject our logic into.
            self._error = self._js_error
            self.available = False
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

    async def close(self):
        await self._cleanup()
        self.available = False

    async def _eval_serp(self, page, k: int) -> dict:
        # Inject via evaluate(eval(src)) rather than add_script_tag: many sites
        # (Bing, Wikipedia) set a Content-Security-Policy that silently blocks
        # injected inline <script> tags, leaving globalThis.__serp undefined.
        # Running the source inside page.evaluate is not subject to that CSP.
        #
        # SAFETY: `src` is our OWN trusted, first-party code — the generated
        # extension/inject/serp.js bundle read from disk at startup (see __init__).
        # It is never user input or remote content, so eval() of it carries no
        # injection risk; it is the deliberate mechanism for CSP-exempt injection.
        return await page.evaluate(
            "({src, k}) => {"
            "  eval(src);"
            "  return globalThis.__serp.detectBlock(document)"
            "    ? {blocked: true}"
            "    : {results: globalThis.__serp.parse(document, k)};"
            "}",
            {"src": self._js["serp"], "k": k},
        )

    async def _eval_extract(self, page) -> dict:
        return await page.evaluate(
            "({readability, extract}) => {"
            "  eval(readability);"
            "  eval(extract);"
            "  return {login: globalThis.__detectLogin(document), content: globalThis.__extract(document)};"
            "}",
            {"readability": self._js["readability"], "extract": self._js["extract"]},
        )

    def _unavailable(self) -> dict:
        return {"status": "error", "driver": "cloak",
                "error": f"cloak browser unavailable: {self._error or 'not started'}"}

    @staticmethod
    async def _safe_close(page):
        if page is None:
            return
        try:
            await page.close()
        except Exception:
            pass

    async def search(self, query: str, k: int = 10, engine: str = "bing") -> dict:
        if not self.available:
            await self.start()
        if not self.available:
            return self._unavailable()
        base = {"query": query, "engine": engine, "driver": "cloak"}
        page = None
        try:
            page = await self._ctx.new_page()
            await page.goto(_serp_url(query, k), timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(SETTLE_MS)
            res = await self._eval_serp(page, k)
            if res.get("blocked"):
                await page.bring_to_front()
                # _page handed to the relay's action registry — intentionally NOT closed.
                return {"status": "action_required", **base, "action": "solve_captcha", "_page": page}
            await page.close()
            results = res.get("results", [])
            return {"status": "ok", **base, "count": len(results), "results": results}
        except Exception as exc:
            await self._safe_close(page)  # don't leak a visible tab on failure
            return {"status": "error", **base, "error": f"{type(exc).__name__}: {exc}"}

    async def fetch(self, url: str, include_html: bool = False) -> dict:
        if not self.available:
            await self.start()
        if not self.available:
            return self._unavailable()
        base = {"url": url, "driver": "cloak"}
        async with self._sem:
            page = None
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
                await self._safe_close(page)  # don't leak a visible tab on failure
                return {"status": "error", **base, "error": f"{type(exc).__name__}: {exc}"}

    async def recheck(self, page, kind: str, payload: dict) -> dict:
        """Re-evaluate a held page after the human acted (resume path)."""
        try:
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
        except Exception as exc:
            await self._safe_close(page)
            driver_base = {"driver": "cloak"}
            if kind == "search":
                driver_base["query"] = payload.get("query")
            else:
                driver_base["url"] = payload.get("url")
            return {"status": "error", **driver_base, "error": f"{type(exc).__name__}: {exc}"}


_driver: CloakDriver | None = None


def get_cloak_driver() -> CloakDriver:
    global _driver
    if _driver is None:
        _driver = CloakDriver()
    return _driver
