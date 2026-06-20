import time

from fastapi import FastAPI

from browser_relay import __version__

EXTENSION_RECENT_POLL_THRESHOLD = 75.0  # seconds; MV3 workers sleep between alarm wakeups

last_poll_time: float = 0.0

app = FastAPI()


def _extension_connected() -> bool:
    if last_poll_time == 0:
        return False
    return (time.monotonic() - last_poll_time) <= EXTENSION_RECENT_POLL_THRESHOLD


@app.get("/version")
async def version():
    return {"version": __version__}


@app.get("/health")
async def health():
    return {"status": "ok", "extension_connected": _extension_connected()}
