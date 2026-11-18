"""Safe startup updater for Neurons.

Only application code is replaced. User data is deliberately outside the
update whitelist:

    data/       profile, memories, chats, update state
    .env        provider keys and local config
    .agents/    installed skills
    .git/       repository metadata

The launcher (`run.py`) applies an available update before importing the
server, so the running process is always using a consistent code tree.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
REPO = "cameronorr2011-beep/neurons"
BRANCH = os.environ.get("NEURONS_UPDATE_BRANCH") or "main"
VERSION_FILE = APP_ROOT / "VERSION"
MAX_ARCHIVE = 30 * 1024 * 1024
ALLOWED_TOP = {
    "server.py", "store.py", "updater.py", "run.py", "plugins.py", "harnesses.py",
    "web", "site", "tests", "requirements.txt", "README.md", "Dockerfile", "VERSION",
}
PROTECTED_TOP = {"data", ".env", ".env.local", ".agents", ".git", "__pycache__"}


def _read_env(name: str) -> str:
    paths = [
        APP_ROOT / ".env",
        Path(os.environ["NEURONS_ENV_FILE"]) if os.environ.get("NEURONS_ENV_FILE") else None,
        APP_ROOT.parent / ".env",
        APP_ROOT.parent / "orrtechnologies" / ".env.local",
    ]
    for path in paths:
        if not path:
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
        except OSError:
            continue
    return os.environ.get(name, "")


def _token() -> str:
    return _read_env("GITHUB_TOKEN") or _read_env("GH_TOKEN")


def _request(url: str, timeout: int = 20, max_bytes: int = MAX_ARCHIVE) -> bytes:
    headers = {"User-Agent": "Neurons-Updater/2.2", "Accept": "application/octet-stream"}
    token = _token()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError("update archive is larger than the safety limit")
    return data


def current_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()[:40] or "0.0.0"
    except OSError:
        return "0.0.0"


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = tuple(int(n) for n in re.findall(r"\d+", str(version))[:4])
    return parts or (0,)


def latest_version() -> str:
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION?ts={int(time.time())}"
    return _request(url, timeout=12, max_bytes=100).decode("utf-8", "replace").strip()[:40]


def check_for_update() -> dict:
    current = current_version()
    try:
        latest = latest_version()
        return {
            "ok": True,
            "current": current,
            "latest": latest,
            "available": _version_tuple(latest) > _version_tuple(current),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "current": current,
            "latest": "",
            "available": False,
            "error": type(exc).__name__ + ": " + str(exc)[:200],
        }


def apply_archive(data: bytes, app_root: Path = APP_ROOT, version: str = "") -> dict:
    """Extract and copy only whitelisted app code into ``app_root``.

    The archive layout from GitHub is ``<repo>-<branch>/...``. Protected
    top-level paths are skipped before anything is written.
    """
    app_root = Path(app_root).resolve()
    data_root = Path(os.environ.get("NEURONS_DATA") or app_root / "data")
    data_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="neurons-update-", dir=str(data_root)))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            if not members:
                raise ValueError("empty update archive")
            prefix = members[0].name.replace("\\", "/").split("/", 1)[0]
            extracted = 0
            for member in members:
                raw = member.name.replace("\\", "/")
                parts = raw.split("/")
                if len(parts) < 2 or parts[0] != prefix:
                    continue
                rel = "/".join(parts[1:])
                top = parts[1]
                if top not in ALLOWED_TOP or top in PROTECTED_TOP:
                    continue
                target = (staging / rel).resolve()
                if staging.resolve() not in target.parents and target != staging.resolve():
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                blob = source.read()
                extracted += len(blob)
                if extracted > MAX_ARCHIVE:
                    raise ValueError("update contents exceed the safety limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)

        required = ("server.py", "web/index.html", "VERSION")
        missing = [name for name in required if not (staging / name).is_file()]
        if missing:
            raise ValueError("update is missing: " + ", ".join(missing))

        for name in ALLOWED_TOP:
            source = staging / name
            if source.is_file():
                shutil.copy2(source, app_root / name)
            elif source.is_dir():
                for path in source.rglob("*"):
                    if not path.is_file():
                        continue
                    target = app_root / path.relative_to(staging)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)

        applied_version = version or current_version()
        state = {
            "version": applied_version,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": f"{REPO}@{BRANCH}",
        }
        (data_root / "update-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def auto_update() -> dict:
    if os.environ.get("NEURONS_UPDATE", "on").lower() in {"0", "off", "false", "no"}:
        return {"ok": True, "updated": False, "disabled": True, "current": current_version()}
    status = check_for_update()
    if not status.get("ok") or not status.get("available"):
        return {**status, "updated": False}
    try:
        archive = _request(
            f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}",
            timeout=45,
        )
        applied = apply_archive(archive, APP_ROOT, status["latest"])
        return {**status, "updated": True, **applied}
    except Exception as exc:
        return {**status, "updated": False, "error": type(exc).__name__ + ": " + str(exc)[:200]}
