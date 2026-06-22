import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from browser_relay import __version__

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 15552
SERVER_URL = os.environ.get("BROWSER_RELAY_URL", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
REQUEST_TIMEOUT = 120.0
BACKEND_STARTUP_TIMEOUT = 10.0
HEALTHCHECK_TIMEOUT = 1.5

AUTO_MANAGE_SERVER = False
MANAGED_BACKEND_PORT: int | None = None

mcp = FastMCP("browser-relay", port=8002)


def _client() -> httpx.AsyncClient:
    """Overridable factory (tests patch this)."""
    return httpx.AsyncClient(base_url=SERVER_URL, timeout=REQUEST_TIMEOUT)


def _state_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = base / "browser-relay"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- backend lifecycle: ported verbatim from google-ai-scraper (renamed) ---


def _backend_lock_path(port: int) -> Path:
    return _state_dir() / f"backend-{port}.lock"


def _backend_log_path(port: int) -> Path:
    return _state_dir() / f"backend-{port}.log"


class _BackendLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.tell() == 0 and self.handle.read(1) == b"":
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)

        return self.handle

    def __exit__(self, exc_type, exc, tb):
        if not self.handle:
            return

        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)

        self.handle.close()


def _manageable_local_port(server_url: str) -> int | None:
    parsed = urlparse(server_url)
    if parsed.scheme not in ("http", ""):
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.path not in ("", "/"):
        return None
    if parsed.query or parsed.fragment:
        return None
    return parsed.port or DEFAULT_PORT


def _backend_healthy(server_url: str) -> bool:
    try:
        resp = httpx.get(f"{server_url.rstrip('/')}/health", timeout=HEALTHCHECK_TIMEOUT)
        if resp.status_code != 200:
            return False
        payload = resp.json()
    except Exception:
        return False
    return payload.get("status") == "ok"


def _wait_for_backend(server_url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _backend_healthy(server_url):
            return True
        time.sleep(0.2)
    return False


def _spawn_backend_process(port: int):
    kwargs = {
        "args": [
            sys.executable,
            "-m",
            "browser_relay.mcp_server.server",
            "--backend",
            "--port",
            str(port),
        ],
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }

    log_path = _backend_log_path(port)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    with log_path.open("ab") as log_file:
        kwargs["stdout"] = log_file
        kwargs["stderr"] = subprocess.STDOUT
        subprocess.Popen(**kwargs)


def _ensure_local_backend(server_url: str, port: int):
    if _backend_healthy(server_url):
        return

    with _BackendLock(_backend_lock_path(port)):
        if _backend_healthy(server_url):
            return

        _spawn_backend_process(port)
        if _wait_for_backend(server_url, BACKEND_STARTUP_TIMEOUT):
            return

    raise RuntimeError(
        f"Could not start shared FastAPI backend at {server_url}. "
        f"Check {_backend_log_path(port)} for details."
    )


def _backend_pid_path(port: int) -> Path:
    return _state_dir() / f"backend-{port}.pid"


def _backend_version(server_url: str) -> str | None:
    """Get the version of the currently running backend, or None if unreachable."""
    try:
        resp = httpx.get(f"{server_url.rstrip('/')}/version", timeout=HEALTHCHECK_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("version")
    except Exception:
        pass
    return None


def _kill_stale_backend(port: int):
    """Kill a running backend whose version doesn't match the current code."""
    pid_path = _backend_pid_path(port)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            # Wait for it to die
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.25)
                except OSError:
                    break
        except (ValueError, OSError):
            pass
        pid_path.unlink(missing_ok=True)


def _kill_backend_by_port(port: int):
    """Last resort: find and kill the backend process listening on a port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
                os.kill(pid, signal.SIGTERM)
            except (ValueError, OSError):
                pass
        # Wait for port to free up
        for _ in range(20):
            try:
                resp = httpx.get(f"http://{DEFAULT_HOST}:{port}/health", timeout=0.5)
                time.sleep(0.25)
            except Exception:
                break
    except Exception:
        pass


def _ensure_backend_current(server_url: str, port: int):
    """If the running backend is outdated, kill and respawn it."""
    running_version = _backend_version(server_url)
    if running_version == __version__:
        return
    if not _backend_healthy(server_url):
        # Not running — _ensure_local_backend will handle spawning
        return
    # Backend is running but version is wrong or missing (no /version endpoint = very old)
    _kill_stale_backend(port)
    # If PID file kill didn't work (old backend without PID file), kill by port
    if _backend_healthy(server_url):
        _kill_backend_by_port(port)
    # Wait for the port to free up
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _backend_healthy(server_url):
            break
        time.sleep(0.25)


def _run_backend(port: int):
    import uvicorn

    from browser_relay.app import app as fastapi_app

    # Write PID file so we can kill stale backends on version mismatch
    pid_path = _backend_pid_path(port)
    pid_path.write_text(str(os.getpid()))

    try:
        asyncio.run(
            uvicorn.Server(
                uvicorn.Config(fastapi_app, host=DEFAULT_HOST, port=port, log_level="warning")
            ).serve()
        )
    finally:
        pid_path.unlink(missing_ok=True)


def _parent_watchdog():
    """Exit when the parent process dies (MCP client session ended).

    When Claude Code exits, `uv run` (our parent) gets killed.  The OS
    reparents us to launchd (PID 1).  Detect that and exit so we don't
    accumulate as zombie MCP stdio servers.
    """
    original_ppid = os.getppid()
    while True:
        time.sleep(5)
        if os.getppid() != original_ppid:
            os._exit(0)


async def _request(method: str, path: str, **kwargs) -> dict:
    """Call the relay, returning a dict (never raises)."""
    try:
        async with _client() as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.TimeoutException:
        return {"status": "error", "error": "Timed out waiting for the browser relay"}
    except httpx.TransportError:
        # Connection-level failure (ConnectError, ConnectTimeout, PoolTimeout, etc.).
        if AUTO_MANAGE_SERVER and MANAGED_BACKEND_PORT is not None:
            try:
                _ensure_local_backend(SERVER_URL, MANAGED_BACKEND_PORT)
                async with _client() as client:
                    resp = await client.request(method, path, **kwargs)
            except Exception:
                return {"status": "error", "error": f"Cannot connect to relay at {SERVER_URL}"}
        else:
            return {"status": "error", "error": f"Cannot connect to relay at {SERVER_URL}"}
    except Exception as exc:  # final backstop — the tool must always return a dict
        return {"status": "error", "error": f"Unexpected relay error: {exc}"}

    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            return {"status": "error", "error": "Relay returned a non-JSON response"}
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return {"status": "error", "error": f"Relay error ({resp.status_code}): {detail}"}


@mcp.tool()
async def search(query: str, k: int = 10, engine: str = "bing", driver: str = "relay") -> str:
    """Web search via the user's browser. Returns {status, results:[{title,url,snippet}]}.

    driver: "relay" (default, the user's logged-in Chrome) or "cloak" (embedded stealth browser for bot-protected/unattended pages).
    """
    result = await _request("GET", "/search", params={"q": query, "k": k, "engine": engine, "driver": driver})
    return json.dumps(result, indent=2)


@mcp.tool()
async def fetch(url: str, include_html: bool = False, driver: str = "relay") -> str:
    """Load a URL in the browser and return its clean readable main content as {status, title, text, ...}.

    driver: "relay" (default, the user's logged-in Chrome) or "cloak" (embedded stealth browser for bot-protected/unattended pages).
    """
    result = await _request(
        "GET", "/fetch",
        params={"url": url, "include_html": str(include_html).lower(), "driver": driver},
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def search_and_fetch(query: str, k: int = 5, engine: str = "bing", driver: str = "relay") -> str:
    """Search, then fetch the full readable text of the top-k results in parallel.

    Returns {status, results:[{title,url,snippet,text,length,fetch_error}]}. A page that
    fails to fetch sets its own fetch_error and text=""; the batch still returns.

    driver: "relay" (default, the user's logged-in Chrome) or "cloak" (embedded stealth browser for bot-protected/unattended pages).
    """
    s = await _request("GET", "/search", params={"q": query, "k": k, "engine": engine, "driver": driver})
    if s.get("status") != "ok":
        return json.dumps(s, indent=2)

    results = s.get("results", [])[:k]

    async def _fetch_one(item: dict) -> dict:
        f = await _request("GET", "/fetch", params={"url": item["url"], "driver": driver})
        if f.get("status") == "ok":
            return {**item, "text": f.get("text", ""), "length": f.get("length", 0), "fetch_error": None}
        if f.get("status") == "action_required":
            # Per spec §18, per-result escalation is non-interactive inside the batch:
            # record it so the caller can fetch(url) individually to drive the handoff.
            return {**item, "text": "", "length": 0,
                    "fetch_error": f"action_required: {f.get('action', 'login')}"}
        return {**item, "text": "", "length": 0, "fetch_error": f.get("error", "fetch failed")}

    merged = await asyncio.gather(*[_fetch_one(r) for r in results])
    return json.dumps(
        {"status": "ok", "query": query, "engine": engine, "driver": driver,
         "count": len(merged), "results": list(merged)},
        indent=2,
    )


@mcp.tool()
async def resume(resume_token: str) -> str:
    """Resume a search/fetch that paused for human action (CAPTCHA or login).

    Call this after the user has solved the challenge / logged in, using the
    resume_token from a previous action_required result. Returns the completed
    result (status "ok"), "action_required" again if still blocked, or "error"
    if the token expired.
    """
    result = await _request("POST", f"/resume/{resume_token}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def health() -> str:
    """Check relay + browser-extension connectivity and queue depth."""
    return json.dumps(await _request("GET", "/health"), indent=2)


def main():
    parser = argparse.ArgumentParser(description="browser-relay MCP server")
    parser.add_argument("--sse", action="store_true")
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--backend", action="store_true")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    global AUTO_MANAGE_SERVER, MANAGED_BACKEND_PORT, SERVER_URL
    if args.backend:
        _run_backend(args.port)
        return

    SERVER_URL = os.environ.get("BROWSER_RELAY_URL", f"http://{DEFAULT_HOST}:{args.port}")
    MANAGED_BACKEND_PORT = _manageable_local_port(SERVER_URL)
    AUTO_MANAGE_SERVER = not args.no_server and MANAGED_BACKEND_PORT is not None
    if AUTO_MANAGE_SERVER:
        try:
            _ensure_backend_current(SERVER_URL, MANAGED_BACKEND_PORT)
            _ensure_local_backend(SERVER_URL, MANAGED_BACKEND_PORT)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    if args.sse:
        mcp.run(transport="sse")
    else:
        threading.Thread(target=_parent_watchdog, daemon=True).start()
        mcp.run()


if __name__ == "__main__":
    main()
