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
        shutil.copyfile(src, DEST / name)
    print(f"synced {len(SRC)} shared JS files into {DEST}")


if __name__ == "__main__":
    main()
