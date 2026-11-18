"""Launch Neurons with a safe startup update check.

Usage:
    py run.py [server options] [--no-update]

An update replaces only app code. `data/`, `.env` and `.agents/` are never
touched. If the check fails, the current version still starts normally.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))


def main() -> None:
    if "--no-update" not in sys.argv:
        try:
            import updater

            result = updater.auto_update()
            if result.get("updated"):
                print(f"Neurons updated to {result.get('version')} (user data preserved)", flush=True)
            elif result.get("error"):
                print(f"update check skipped: {result['error']}", flush=True)
        except Exception as exc:
            print(f"update check skipped: {type(exc).__name__}: {str(exc)[:160]}", flush=True)

    args = [arg for arg in sys.argv[1:] if arg != "--no-update"]
    sys.argv = [str(APP_ROOT / "server.py"), *args]
    runpy.run_path(str(APP_ROOT / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
