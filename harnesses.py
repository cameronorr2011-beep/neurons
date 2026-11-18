"""Local coding-agent harness adapters.

These are deliberately a catalog of known CLIs, not an arbitrary shell. A
harness runs only when its executable is present and its adapter is invoked
from the app. Keys save through the UI into a user-only local file; they are
never returned to the browser.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
MAX_OUTPUT = 30_000
TIMEOUT = 600
_lock = threading.RLock()
_keys_path = Path(os.environ.get("NEURONS_DATA") or APP_ROOT / "data") / "harnesses.json"


def configure(data_dir: str | Path) -> None:
    global _keys_path
    with _lock:
        _keys_path = Path(data_dir) / "harnesses.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_keys() -> dict:
    try:
        data = json.loads(_keys_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_keys(data: dict) -> None:
    _keys_path.parent.mkdir(parents=True, exist_ok=True)
    temp = _keys_path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, _keys_path)


def set_key(harness_id: str, key: str) -> None:
    item = _entry(harness_id)
    if not item.get("auth_env"):
        raise ValueError(f"{item['name']} does not use an API key")
    key = str(key or "").strip()
    if not key:
        raise ValueError("API key is required")
    with _lock:
        data = _load_keys()
        keys = data.setdefault("keys", {})
        keys[harness_id] = {"key": key[:8000], "updated_at": _now()}
        _save_keys(data)


def clear_key(harness_id: str) -> None:
    _entry(harness_id)
    with _lock:
        data = _load_keys()
        keys = data.get("keys")
        if isinstance(keys, dict):
            keys.pop(harness_id, None)
            _save_keys(data)


def _stored_key(harness_id: str) -> str:
    with _lock:
        value = (_load_keys().get("keys") or {}).get(harness_id) or {}
    return str(value.get("key") or "")


HARNESSES = [
    {
        "id": "claude-code", "name": "Claude Code", "vendor": "Anthropic",
        "command": "claude", "args": ["-p", "{prompt}"],
        "auth_env": "ANTHROPIC_API_KEY", "auth_label": "Anthropic API key",
        "training_exempt": True,
        "docs": "https://docs.anthropic.com/en/docs/claude-code",
        "description": "Anthropic's terminal coding agent. Uses your Claude Code login or Anthropic API key.",
    },
    {
        "id": "codex", "name": "Codex", "vendor": "OpenAI",
        "command": "codex", "args": ["exec", "{prompt}"],
        "auth_env": "OPENAI_API_KEY", "auth_label": "OpenAI API key",
        "training_exempt": True,
        "docs": "https://developers.openai.com/codex/cli/",
        "description": "OpenAI's local coding agent. Uses your existing Codex login or OpenAI API key.",
    },
    {
        "id": "antigravity", "name": "Antigravity", "vendor": "Google",
        "command": "antigravity", "args": ["{prompt}"],
        "auth_env": "GEMINI_API_KEY", "auth_label": "Gemini API key",
        "training_exempt": True,
        "docs": "https://antigravity.google/",
        "description": "Google's agentic development environment. Adapter is experimental.",
        "experimental": True,
    },
    {
        "id": "freebuff", "name": "Freebuff", "vendor": "Freebuff",
        "command": "freebuff", "args": ["{prompt}"],
        "auth_env": "FREEBUFF_API_KEY", "auth_label": "Freebuff API key",
        "training_exempt": True,
        "docs": "https://freebuff.com/",
        "description": "Run a local Freebuff-compatible CLI when one is installed.",
        "experimental": True,
    },
    {
        "id": "hermes", "name": "Hermes", "vendor": "Nous Research",
        "command": "hermes", "args": ["{prompt}"],
        "auth_env": "", "training_exempt": True,
        "docs": "https://github.com/NousResearch/hermes-agent",
        "description": "Run Hermes Agent in one-shot mode from the app.",
    },
    {
        "id": "aider", "name": "Aider", "vendor": "Aider",
        "command": "aider", "args": ["--message", "{prompt}", "--yes-always", "--no-auto-commits"],
        "auth_env": "OPENAI_API_KEY", "auth_label": "OpenAI API key",
        "training_exempt": True,
        "docs": "https://aider.chat/docs/",
        "description": "Pair-programming harness for a local git repository.",
    },
    {
        "id": "opencode", "name": "OpenCode", "vendor": "OpenCode",
        "command": "opencode", "args": ["run", "{prompt}"],
        "auth_env": "", "training_exempt": True,
        "docs": "https://opencode.ai/docs/",
        "description": "Open-source terminal coding agent.",
    },
    {
        "id": "gemini-cli", "name": "Gemini CLI", "vendor": "Google",
        "command": "gemini", "args": ["-p", "{prompt}"],
        "auth_env": "GEMINI_API_KEY", "auth_label": "Gemini API key",
        "training_exempt": True,
        "docs": "https://github.com/google-gemini/gemini-cli",
        "description": "Google's open-source terminal agent.",
    },
    {
        "id": "cursor-agent", "name": "Cursor Agent", "vendor": "Cursor",
        "command": "cursor-agent", "args": ["-p", "{prompt}"],
        "auth_env": "CURSOR_API_KEY", "auth_label": "Cursor API key",
        "training_exempt": True,
        "docs": "https://docs.cursor.com/",
        "description": "Cursor's CLI agent, when installed.",
        "experimental": True,
    },
]


def _entry(harness_id: str) -> dict:
    for item in HARNESSES:
        if item["id"] == harness_id:
            return item
    raise ValueError("unknown harness")


def catalog(env_value=lambda name: "") -> list[dict]:
    result = []
    for item in HARNESSES:
        auth = item.get("auth_env") or ""
        saved = bool(_stored_key(item["id"]))
        from_env = bool(auth and env_value(auth))
        result.append({
            **{key: value for key, value in item.items() if key != "args"},
            "installed": bool(shutil.which(item["command"])),
            "auth_configured": bool(auth and (saved or from_env)),
            "key_saved": saved,
            "key_source": "saved" if saved else ("env" if from_env else ""),
            "setup": (f"add {item.get('auth_label') or auth}" if auth else f"install {item['command']}"),
        })
    return result


def run(harness_id: str, prompt: str, cwd: str | Path, env_value=lambda name: "") -> dict:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    item = _entry(harness_id)
    found = shutil.which(item["command"])
    if not found:
        raise ValueError(f"{item['name']} is not installed ({item['command']} not found on PATH)")
    args = [part.replace("{prompt}", prompt) for part in item["args"]]
    env = os.environ.copy()
    auth = item.get("auth_env")
    if auth:
        value = _stored_key(item["id"]) or env_value(auth)
        if value:
            env[auth] = value
    proc = subprocess.run(
        [found, *args],
        cwd=str(Path(cwd)),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=TIMEOUT,
        env=env,
    )
    output = (proc.stdout or "")
    if (proc.stderr or "").strip():
        output += ("\n[stderr]\n" if output else "") + proc.stderr
    output = output.strip() or "(harness returned no output)"
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + f"\n… [truncated, {len(output)} chars total]"
    return {
        "id": item["id"], "name": item["name"], "exit_code": proc.returncode,
        "output": output, "command": found,
    }
