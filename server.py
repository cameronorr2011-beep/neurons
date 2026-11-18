"""
Neurons
=======
A local-first agent harness: streaming chat with visible reasoning, project
tools, skills, MCP servers and subagents, all served from your own machine.

Run:
    py server.py --open
Then open http://127.0.0.1:8770

The API key stays on the server; the browser never receives it. Binds to
loopback by default. Profile, memory and conversations live in data/neurons.db.
"""

from __future__ import annotations

import argparse
import json
import os
import base64
import html
import platform
import ctypes
import ctypes.wintypes
import queue
import re
import shutil
import socket
import tempfile
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import store

APP_ROOT = Path(__file__).resolve().parent


def _read_env_file(path: Path) -> dict:
    data = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


# App-local .env wins, then an explicit NEURONS_ENV_FILE, then the legacy
# workspace locations so an existing local install keeps working.
_CONFIG_ENV = _read_env_file(APP_ROOT / ".env")
_ENV_PATHS = [
    os.environ.get("NEURONS_ENV_FILE"),
    APP_ROOT.parent / ".env",
    APP_ROOT.parent / "orrtechnologies" / ".env.local",
]
for _env_path in _ENV_PATHS:
    if not _env_path:
        continue
    for _key, _value in _read_env_file(Path(_env_path)).items():
        _CONFIG_ENV.setdefault(_key, _value)


def env_value(name: str) -> str:
    """Config files beat the inherited shell: a stale exported key must not
    shadow the one the operator actually configured."""
    return _CONFIG_ENV.get(name) or os.environ.get(name) or ""


WORKSPACE_ROOT = Path(env_value("NEURONS_WORKSPACE") or APP_ROOT).resolve()
ROOT = WORKSPACE_ROOT


DEFAULT_PROVIDER_MODELS = {
    "numes": "dealignai/deepseek-v4.1-flash-uncensored-fp8:free",
    "logfare": "logfare/auto",
}


def provider_defs() -> dict:
    """Every configured OpenAI-compatible provider. Keys stay server-side."""
    defs: dict = {}
    numes_key = env_value("NUMES_API_KEY")
    if numes_key:
        defs["numes"] = {
            "label": "Numes",
            "base": (env_value("NUMES_BASE_URL") or "https://api.numes.ai/api/v1").rstrip("/"),
            "key": numes_key,
            "model": env_value("NUMES_MODEL") or DEFAULT_PROVIDER_MODELS["numes"],
        }
    logfare_key = env_value("LOGFARE_API_KEY")
    if logfare_key:
        defs["logfare"] = {
            "label": "Logfare",
            "base": (env_value("LOGFARE_BASE_URL") or "https://logfare.ai/v1").rstrip("/"),
            "key": logfare_key,
            "model": env_value("LOGFARE_MODEL") or DEFAULT_PROVIDER_MODELS["logfare"],
        }
    custom_key = env_value("NEURONS_API_KEY")
    custom_base = env_value("NEURONS_BASE_URL")
    if custom_key and custom_base and not any(
        d["key"] == custom_key and d["base"] == custom_base.rstrip("/") for d in defs.values()
    ):
        defs["custom"] = {
            "label": "Custom",
            "base": custom_base.rstrip("/"),
            "key": custom_key,
            "model": env_value("NEURONS_MODEL") or "",
        }
    return defs


def default_provider_id() -> str:
    defs = provider_defs()
    wanted = env_value("NEURONS_PROVIDER")
    if wanted in defs:
        return wanted
    return next(iter(defs), "")


def fallback_provider_id(exclude: str) -> str:
    defs = provider_defs()
    wanted = env_value("NEURONS_FALLBACK")
    if wanted in defs and wanted != exclude:
        return wanted
    return next((pid for pid in defs if pid != exclude), "")


def resolve_provider(provider_id: str | None = None):
    defs = provider_defs()
    if not defs:
        raise ValueError("no provider configured: set NUMES_API_KEY or LOGFARE_API_KEY")
    pid = provider_id or default_provider_id()
    if pid not in defs:
        raise ValueError(f"unknown provider: {pid}")
    return pid, defs[pid]


def ensure_model(pid: str, prov: dict, model: str) -> str:
    """Reject a model ID that belongs to another provider's catalogue.

    Uses the cached catalogue only, so a chat never waits on a model list.
    An empty cache means we cannot judge, so the request proceeds.
    """
    if not model:
        return prov["model"]
    with _models_lock:
        entry = _MODELS_CACHE.get(pid)
    if not entry:
        return model
    ids = {m.get("id") for m in entry["data"].get("data", [])}
    if ids and model not in ids:
        raise ValueError(
            f"model '{model}' is not in {prov['label']}'s catalogue — "
            f"refresh and pick a model for that provider"
        )
    return model


DEFAULT_PROVIDER = default_provider_id()
_DEFAULT_PROV = provider_defs().get(DEFAULT_PROVIDER) or {}
API_KEY = _DEFAULT_PROV.get("key", "")
BASE_URL = _DEFAULT_PROV.get("base", "")
DEFAULT_MODEL = _DEFAULT_PROV.get("model", DEFAULT_PROVIDER_MODELS["logfare"])

SYSTEM_PROMPT = (
    "You are Neurons, an agent running on the user's own computer with full access to it. "
    "run_command executes real commands in the shell and the file tools reach any path, so "
    "use absolute paths instead of guessing. "
    "Prefer acting over asking: inspect, run, read what you need, then report what actually happened. "
    "You can also drive the desktop: click, type_text, press_key, scroll, list/focus/close windows "
    "and read or write the clipboard. Always pass type_text/press_key a 'window' title so keystrokes "
    "cannot land in the wrong app, and take a screenshot after a GUI action to check it worked. You can also write files (write_file), find files by glob (find_files), fetch URLs (http_get), launch apps or URLs (launch_app), inspect processes and system stats, and control volume."
)

STYLE_HINTS = {
    "concise": "Keep answers short and direct; lead with the result.",
    "balanced": "Be clear and useful, with enough detail to act on.",
    "detailed": "Explain your reasoning and include the important details.",
    "direct": "Be blunt, practical and action-oriented; skip filler.",
}


def personalized_messages(messages: list) -> list:
    """Prepend the user's profile and durable memories to the model context.

    Profile and memories are stored server-side, so they survive a browser
    reset and are never trusted from the client.
    """
    base = ""
    rest = []
    for message in messages or []:
        if message.get("role") == "system" and isinstance(message.get("content"), str) and not base:
            base = message["content"]
        elif message.get("role") != "system":
            rest.append(message)
    profile = store.get_profile()
    memories = store.list_memories()
    system = base or SYSTEM_PROMPT
    context = []
    if profile.get("name"):
        context.append(f"The user's name is {profile['name']}.")
    if profile.get("about"):
        context.append(f"About the user: {profile['about'][:2000]}")
    hint = STYLE_HINTS.get(profile.get("style") or "balanced")
    if hint:
        context.append(f"Preferred response style: {hint}")
    if memories:
        facts = "\n".join(f"- {m['text'][:500]}" for m in memories[:50])
        context.append("Durable memories from previous conversations (use when relevant):\n" + facts)
    context.append(
        "When the user shares a lasting preference, personal fact or project detail, "
        "call the remember tool so it survives this conversation. Do not remember transient commands."
    )
    return [{"role": "system", "content": system + "\n\n" + "\n\n".join(context)}] + rest

# ---------------------------------------------------------------------------
# Local tools (this machine)
# ---------------------------------------------------------------------------

MAX_READ_CHARS = 24_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "chroma_store", "chroma_test_db"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders in a directory inside the project working tree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path, e.g. 'tools' or '.'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the project. Credential files are refused.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path, e.g. 'tools/logfare_web.py'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search project text files for a literal substring; returns matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to look for"},
                    "path": {"type": "string", "description": "Directory to search, default '.'"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List installed skills (.agents/skills/*/SKILL.md) with their descriptions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load the full SKILL.md for a skill so you can follow its instructions.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill folder name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command on this machine (git-bash). Returns exit code, stdout and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command line, POSIX syntax"},
                    "cwd": {"type": "string", "description": "Working directory: absolute (C:/Users/...) or relative to the workspace root; default '.'"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the public web and return titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Capture the user's screen as an image so you can look at it.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text as real keystrokes (Unicode-safe). Pass 'window' to focus that window first; it refuses if no window matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "window": {"type": "string", "description": "Window title substring to focus before typing"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a key or combo, e.g. 'enter', 'ctrl+s', 'alt+tab', 'f5'. Pass 'window' to focus it first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                    "window": {"type": "string", "description": "Window title substring to focus before pressing"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Move the mouse (optional) and click. Take a screenshot first to find coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "clicks": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the mouse wheel. Positive is up, negative is down.",
            "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows",
            "description": "List visible windows, or focus/minimize/maximize/close one by title substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "focus", "minimize", "maximize", "close"]},
                    "title": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "Read or write the system clipboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "text": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "Spawn a subagent with the same tools for an independent subtask. Call it multiple times for parallel work; returns only the subagent's final answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Self-contained instruction for the subagent"},
                    "model": {"type": "string", "description": "Optional model id, default the provider's model"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a durable fact, preference or project detail about the user to long-term memory. Use sparingly; do not store transient commands.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "One concise fact to remember"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file anywhere on this machine, optionally appending. Credential files are refused.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path or workspace-relative path"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean", "description": "Append instead of overwriting"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files by glob pattern (e.g. '*.py') under a directory; newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory to search, default the workspace"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Fetch an http(s) URL and return its readable text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webhook",
            "description": "POST a JSON payload to an http(s) webhook and return the HTTP status and response body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "data": {"type": "object", "description": "JSON payload to send"},
                    "headers": {"type": "object", "description": "Optional extra headers"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_app",
            "description": "Launch an application, document or URL with the OS default handler.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "exe name, absolute path or http(s) URL"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "processes",
            "description": "List running processes by memory use (largest first).",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Report OS, CPU, memory, disks, battery and uptime for this machine.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "volume",
            "description": "Change system volume: up, down, or mute.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["up", "down", "mute"]},
                    "steps": {"type": "integer", "description": "How many 2% steps; mute toggles this many times"},
                },
                "required": ["action"],
            },
        },
    },
]


def _resolve(rel: str) -> Path:
    """Resolve a path anywhere on this machine.

    Absolute paths, ~ and workspace-relative paths all work; nothing is jailed to
    the project any more. Credential files are still refused so a stray
    read_file cannot dump secrets into the model's context by accident.
    """
    raw = (rel or ".").strip().strip('"').strip("'")
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    target = candidate.resolve()
    name = target.name.lower()
    if name.startswith(".env") or target.suffix.lower() in {".pem", ".key", ".pfx"}:
        raise ValueError("refusing to touch credential files")
    return target


def _tool_list_dir(args: dict) -> str:
    target = _resolve(args.get("path", "."))
    if not target.is_dir():
        return f"not a directory: {target}"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = [("[dir] " if p.is_dir() else "      ") + p.name for p in entries[:200]]
    return "\n".join(lines) or "(empty)"


def _tool_read_file(args: dict) -> str:
    target = _resolve(args.get("path", ""))
    if not target.is_file():
        return f"not a file: {args.get('path')}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n… [truncated, {len(text)} chars total]"
    return text


def _tool_search_files(args: dict) -> str:
    needle = args.get("text") or ""
    if not needle:
        return "no search text given"
    base = _resolve(args.get("path", "."))
    hits: list[str] = []
    for path in base.rglob("*"):
        if len(hits) >= 40:
            break
        if not path.is_file() or path.suffix.lower() in {".pyc", ".png", ".jpg", ".zip", ".exe"}:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.lower().startswith(".env"):
            continue
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if needle in line:
                    hits.append(f"{path}:{i}: {line.strip()[:160]}")
                    break
        except OSError:
            continue
    return "\n".join(hits) or "no matches"


def _skills_root() -> Path:
    return Path(env_value("NEURONS_SKILLS") or APP_ROOT / ".agents" / "skills")


SKILL_REPOS = (
    ("addyosmani", "web-quality-skills"),
    ("vercel-labs", "agent-skills"),
    ("anthropics", "skills"),
)
_SKILL_INSTALL_LOCK = threading.Lock()
_SKILL_FILE_MAX = 2 * 1024 * 1024
_SKILL_TOTAL_MAX = 80 * 1024 * 1024


def _github_request(url: str, timeout: int = 30) -> bytes:
    headers = {"User-Agent": "Neurons/1.0", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _github_json(url: str) -> dict:
    return json.loads(_github_request(url).decode("utf-8", "replace"))


def _github_raw(owner: str, repo: str, branch: str, path: str) -> bytes:
    quoted = urllib.parse.quote(path, safe="/")
    return _github_request(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{quoted}")


def install_all_skills(overwrite: bool = False) -> dict:
    """Download every skill from the public catalog into .agents/skills."""
    root = _skills_root()
    root.mkdir(parents=True, exist_ok=True)
    installed, skipped, errors = [], [], []
    seen = set()
    budget = _SKILL_TOTAL_MAX
    with _SKILL_INSTALL_LOCK:
        for owner, repo in SKILL_REPOS:
            try:
                meta = _github_json(f"https://api.github.com/repos/{owner}/{repo}")
                branch = meta.get("default_branch") or "main"
                tree = _github_json(
                    f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
                )
                if tree.get("truncated"):
                    errors.append(f"{owner}/{repo}: tree truncated; skipped")
                    continue
                blobs = {
                    item["path"]: item
                    for item in tree.get("tree", [])
                    if item.get("type") == "blob"
                }
                for skill_md in sorted(path for path in blobs if path.endswith("/SKILL.md")):
                    skill_dir = skill_md[: -len("/SKILL.md")]
                    folder = skill_dir.rsplit("/", 1)[-1]
                    try:
                        md_bytes = _github_raw(owner, repo, branch, skill_md)
                    except Exception as exc:
                        errors.append(f"{owner}/{repo}/{folder}: {str(exc)[:120]}")
                        continue
                    match = re.search(
                        r"(?m)^name:\s*[\"']?([^\"'\n]+)",
                        md_bytes.decode("utf-8", "replace"),
                    )
                    name = (match.group(1).strip() if match else folder)[:80]
                    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                        errors.append(f"{owner}/{repo}/{folder}: unsafe name {name!r}")
                        continue
                    if name in seen:
                        skipped.append(name + " (duplicate)")
                        continue
                    dest = root / name
                    if dest.exists() and not overwrite:
                        skipped.append(name)
                        seen.add(name)
                        continue
                    payload = []
                    for path in sorted(p for p in blobs if p.startswith(skill_dir + "/")):
                        size = int(blobs[path].get("size") or 0)
                        label = path.rsplit("/", 1)[-1]
                        if size > _SKILL_FILE_MAX:
                            errors.append(f"{name}: skipped large file {label}")
                            continue
                        if path == skill_md:
                            data = md_bytes
                        else:
                            try:
                                data = _github_raw(owner, repo, branch, path)
                            except Exception as exc:
                                errors.append(f"{name}: {label}: {str(exc)[:100]}")
                                continue
                        if len(data) > _SKILL_FILE_MAX:
                            errors.append(f"{name}: skipped large file {label}")
                            continue
                        if len(data) > budget:
                            errors.append("download budget exhausted")
                            return {"installed": installed, "skipped": skipped, "errors": errors}
                        budget -= len(data)
                        payload.append((path[len(skill_dir) + 1:], data))
                    if not payload:
                        errors.append(f"{name}: no downloadable files")
                        continue
                    for rel, data in payload:
                        target = (dest / rel).resolve()
                        if root.resolve() not in target.parents:
                            errors.append(f"{name}: unsafe path {rel}")
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                    installed.append(name)
                    seen.add(name)
            except Exception as exc:
                errors.append(f"{owner}/{repo}: {str(exc)[:160]}")
    return {"installed": sorted(installed), "skipped": sorted(set(skipped)), "errors": errors}


def _tool_list_skills(args: dict) -> str:
    root = _skills_root()
    if not root.is_dir():
        return "no .agents/skills directory"
    out = []
    for skill in sorted(root.iterdir()):
        md = skill / "SKILL.md"
        if not md.is_file():
            continue
        desc = ""
        for line in md.read_text(encoding="utf-8", errors="ignore").splitlines()[:20]:
            if line.lower().startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"')
                break
        out.append(f"{skill.name} — {desc[:140]}")
    return "\n".join(out) or "no skills found"


def _tool_load_skill(args: dict) -> str:
    name = (args.get("name") or "").strip()
    md = _skills_root() / name / "SKILL.md"
    if not md.is_file():
        return f"unknown skill: {name}"
    text = md.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_READ_CHARS]


def _tool_web_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "no query given"
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", "replace")

    blocks = re.findall(
        r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?result__snippet"[^>]*>(.*?)</a>',
        body, re.S,
    )
    if not blocks:
        return "search provider returned no parseable results (may be rate-limiting this IP)"

    def clean(fragment: str) -> str:
        text = re.sub(r"<[^>]+>", "", fragment)
        return " ".join(text.split())[:220]

    out = []
    for href, title, snippet in blocks[:8]:
        if "duckduckgo.com/l/?uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        out.append(f"- {clean(title)}\n  {href}\n  {clean(snippet)}")
    return "\n".join(out)


ALLOW_SCREEN = True
ALLOW_SHELL = True
SHELL_TIMEOUT = 600
SHELL_MAX_OUTPUT = 8000

# Catastrophic patterns only: this is a guard rail, not a sandbox. Anything
# that reaches outside the workspace still runs as you.
SHELL_DENY = (
    "rm -rf /", "rm -rf ~", "mkfs", "format c:", "diskpart", "shutdown",
    "reboot", ":(){ ", "git push --force", "git reset --hard", "reg delete",
)


def _tool_run_command(args: dict) -> str:
    if not ALLOW_SHELL:
        return "shell is disabled; restart without --no-shell to enable it."
    command = (args.get("command") or "").strip()
    if not command:
        return "no command given"
    lowered = command.lower()
    for bad in SHELL_DENY:
        if bad in lowered:
            return f"refused: command matches a destructive pattern ({bad!r})"

    try:
        cwd = _resolve(args.get("cwd") or ".")
    except ValueError as exc:
        return f"refused: {exc}"
    if not cwd.is_dir():
        return f"not a directory: {args.get('cwd')}"

    bash = shutil.which("bash")
    try:
        proc = subprocess.run(
            [bash, "-lc", command] if bash else command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SHELL_TIMEOUT,
            shell=(bash is None),
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {SHELL_TIMEOUT}s"
    except OSError as exc:
        return f"could not start the command: {exc}"

    out = proc.stdout or ""
    if (proc.stderr or "").strip():
        out += "\n[stderr]\n" + proc.stderr
    out = out.strip() or "(no output)"
    if len(out) > SHELL_MAX_OUTPUT:
        out = out[:SHELL_MAX_OUTPUT] + f"\n… [truncated, {len(out)} chars total]"
    return f"exit={proc.returncode}\n{out}"


# ---------------------------------------------------------------------------
# Desktop control: mouse, keyboard, windows, clipboard (Windows, no deps)
# ---------------------------------------------------------------------------

_VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class _INPUT(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_ulong),
        ("_pad0", ctypes.c_ulong),
        ("ki", _KEYBDINPUT),
        ("_pad1", ctypes.c_ubyte * 8),
    )


def _user32():
    if sys.platform != "win32":
        raise RuntimeError("desktop control is Windows-only")
    return ctypes.windll.user32


def _cursor_text() -> str:
    point = ctypes.wintypes.POINT()
    _user32().GetCursorPos(ctypes.byref(point))
    return f"{point.x},{point.y}"


def _windows_list() -> list:
    u = _user32()
    found: list = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if u.IsWindowVisible(hwnd):
            length = u.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                u.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title:
                    found.append((hwnd, title))
        return True

    u.EnumWindows(callback_type(collect), 0)
    return found


def _find_window(title: str):
    needle = (title or "").strip()
    if needle.isdigit():
        handle = int(needle)
        if ctypes.windll.user32.IsWindow(handle):
            length = ctypes.windll.user32.GetWindowTextLengthW(handle)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(handle, buf, length + 1)
            return handle, buf.value or f"hwnd {handle}"
        return None, None
    needle = needle.lower()
    for hwnd, current in _windows_list():
        if needle and needle in current.lower():
            return hwnd, current
    return None, None


def _tool_click(args: dict) -> str:
    u = _user32()
    if args.get("x") is not None and args.get("y") is not None:
        u.SetCursorPos(int(args["x"]), int(args["y"]))
    button = (args.get("button") or "left").lower()
    down, up = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }.get(button, (0x0002, 0x0004))
    times = max(1, min(int(args.get("clicks") or 1), 3))
    for _ in range(times):
        u.mouse_event(down, 0, 0, 0, 0)
        u.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.08)
    return f"{button} click x{times} at {_cursor_text()}"


def _tool_scroll(args: dict) -> str:
    amount = max(-20, min(int(args.get("amount") or -3), 20))
    _user32().mouse_event(0x0800, 0, 0, amount * 120, 0)
    return f"scrolled {amount} notches at {_cursor_text()}"


def _focus_window(title: str):
    """Bring a window to the front. Returns an error string if it is not there."""
    hwnd, found = _find_window(title)
    if not hwnd:
        return f"refused: no visible window matching {title!r}; nothing was typed or pressed"
    u = _user32()
    u.ShowWindow(hwnd, 9)
    u.SwitchToThisWindow(hwnd, True)
    time.sleep(0.3)
    return None


def _tool_type_text(args: dict) -> str:
    u = _user32()
    window = (args.get("window") or "").strip()
    if window:
        problem = _focus_window(window)
        if problem:
            return problem
    text = args.get("text") or ""
    if not text:
        return "no text given"
    sent = 0
    for char in text:
        if char == "\n":
            u.keybd_event(_VK["enter"], 0, 0, 0)
            u.keybd_event(_VK["enter"], 0, 0x0002, 0)
            sent += 1
            continue
        for flags in (0x0004, 0x0004 | 0x0002):
            stroke = _INPUT(type=1, ki=_KEYBDINPUT(0, ord(char), flags, 0, None))
            u.SendInput(1, ctypes.byref(stroke), ctypes.sizeof(_INPUT))
        sent += 1
        time.sleep(0.004)
    return f"typed {sent} characters into the focused window"


def _tool_press_key(args: dict) -> str:
    u = _user32()
    window = (args.get("window") or "").strip()
    if window:
        problem = _focus_window(window)
        if problem:
            return problem
    combo = (args.get("keys") or "").strip().lower()
    if not combo:
        return "no keys given"
    codes = []
    for part in [p.strip() for p in combo.split("+") if p.strip()]:
        if part in _VK:
            codes.append(_VK[part])
        elif len(part) == 1:
            codes.append(ord(part.upper()))
        else:
            return f"unknown key: {part}"
    for code in codes:
        u.keybd_event(code, 0, 0, 0)
        time.sleep(0.03)
    for code in reversed(codes):
        u.keybd_event(code, 0, 0x0002, 0)
        time.sleep(0.03)
    return f"pressed {combo}"


def _tool_windows(args: dict) -> str:
    u = _user32()
    action = (args.get("action") or "list").lower()
    if action == "list":
        items = _windows_list()
        return "\n".join(f"{hwnd}  {title}" for hwnd, title in items[:60]) or "(no visible windows)"
    hwnd, found = _find_window(args.get("title") or "")
    if not hwnd:
        return f"no visible window matching {args.get('title')!r}"
    if action == "focus":
        u.ShowWindow(hwnd, 9)
        u.SwitchToThisWindow(hwnd, True)
        return f"focused {found!r} at {_cursor_text()}"
    if action == "minimize":
        u.ShowWindow(hwnd, 6)
        return f"minimized {found!r}"
    if action == "maximize":
        u.ShowWindow(hwnd, 3)
        return f"maximized {found!r}"
    if action == "close":
        u.PostMessageW(hwnd, 0x0010, 0, 0)
        return f"asked {found!r} to close"
    return f"unknown action: {action}"


def _clipboard_api():
    u = _user32()
    k32 = ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = (ctypes.c_void_p,)
    k32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    return u, k32


def _tool_clipboard(args: dict) -> str:
    u, k32 = _clipboard_api()
    action = (args.get("action") or "get").lower()
    if not u.OpenClipboard(None):
        return "clipboard is busy, try again"
    try:
        if action == "get":
            handle = u.GetClipboardData(13)
            if not handle:
                return "(clipboard is empty or not text)"
            pointer = k32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(pointer)[:4000]
            finally:
                k32.GlobalUnlock(handle)
        text = args.get("text") or ""
        data = text.encode("utf-16-le") + b"\x00\x00"
        u.EmptyClipboard()
        handle = k32.GlobalAlloc(0x0002, len(data))
        pointer = k32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        k32.GlobalUnlock(handle)
        u.SetClipboardData(13, handle)
        return f"clipboard set to {len(text)} characters"
    finally:
        u.CloseClipboard()


def _tool_screenshot(args: dict) -> str:
    if not ALLOW_SCREEN:
        return "screen capture is off. Restart the server with --allow-screen to enable it."
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    path = Path(tempfile.gettempdir()) / f"neurons-screen-{int(time.time())}.png"
    image.save(path)
    return f"SCREENSHOT:{path}|{image.width}x{image.height}"


_SUBAGENT_DEPTH = threading.local()


def _tool_delegate(args: dict) -> str:
    """Run a focused subagent with the full toolset and return its answer."""
    task = (args.get("task") or "").strip()
    if not task:
        return "no task given"
    depth = getattr(_SUBAGENT_DEPTH, "depth", 0)
    if depth >= 2:
        return "subagent depth limit reached (2); finish this task in the current agent"
    try:
        pid, prov = resolve_provider(args.get("provider"))
        model = ensure_model(pid, prov, args.get("model") or prov["model"])
    except Exception as exc:
        return f"subagent could not start: {str(exc)[:200]}"
    _SUBAGENT_DEPTH.depth = depth + 1
    try:
        result = run_agent(
            [
                {"role": "system", "content": "You are a focused subagent. Use tools when they help, and call finish with the final answer when done."},
                {"role": "user", "content": task},
            ],
            model=model, max_steps=12, provider=pid,
        )
    except Exception as exc:
        return f"subagent failed: {str(exc)[:300]}"
    finally:
        _SUBAGENT_DEPTH.depth = depth
    text = (result.get("text") or "").strip() or "(subagent returned no text)"
    steps = result.get("steps") or []
    return f"{text}\n\n[subagent {pid} · {len(steps)} tool step(s)]"[:4000]


# ---------------------------------------------------------------------------
# MCP client (stdio JSON-RPC), servers discovered from .mcp.json
# ---------------------------------------------------------------------------

MCP_CONFIG = Path(env_value("NEURONS_MCP_CONFIG") or APP_ROOT / ".mcp.json")
MCP_TIMEOUT = 20.0
MCP_MAX_TOOLS = 40
_MCP_CLIENTS: dict = {}
_MCP_TOOLS: list = []
_MCP_ERRORS: dict = {}
_mcp_started = False
_mcp_lock = threading.Lock()


class McpClient:
    """Minimal MCP stdio client: newline-delimited JSON-RPC 2.0."""

    def __init__(self, name: str, conf: dict) -> None:
        self.name = name
        self.conf = conf
        self.proc = None
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.next_id = 1

    def start(self) -> None:
        env = dict(**{k: v for k, v in __import__("os").environ.items()})
        env.update(self.conf.get("env") or {})
        self.proc = subprocess.Popen(
            [self.conf["command"], *self.conf.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "logfare-web", "version": "1.0"},
        })
        self.notify("notifications/initialized")

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.inbox.put(json.loads(line))
            except Exception:
                continue

    def _send(self, payload: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None, timeout: float = MCP_TIMEOUT) -> dict:
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                raise RuntimeError(str(msg["error"])[:200])
            return msg.get("result") or {}
        raise TimeoutError(f"{self.name}: {method} timed out")

    def list_tools(self) -> list:
        return self.request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, args: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": args})
        parts = []
        for item in result.get("content", []) or []:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(f"[{item.get('type')} content]")
        return "\n".join(parts)[:8000] or "(empty result)"

    def close(self) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


def _mcp_tool_schema(server: str, tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"mcp__{server}__{tool['name']}",
            "description": (tool.get("description") or f"{tool['name']} on MCP server {server}")[:300],
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


def mcp_connect_all() -> None:
    global _mcp_started
    with _mcp_lock:
        if _mcp_started:
            return
        _mcp_started = True

    try:
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        _MCP_ERRORS[".mcp.json"] = f"cannot read: {exc}"[:200]
        return

    for name, conf in (config.get("mcpServers") or {}).items():
        if conf.get("type") == "http" or conf.get("url"):
            _MCP_ERRORS[name] = "http transport not implemented yet"
            continue
        try:
            client = McpClient(name, conf)
            client.start()
            tools = client.list_tools()
            _MCP_CLIENTS[name] = client
            for tool in tools[:MCP_MAX_TOOLS]:
                _MCP_TOOLS.append({**_mcp_tool_schema(name, tool), "server": name, "tool": tool["name"]})
            print(f"mcp: {name} connected, {len(tools)} tools", flush=True)
        except Exception as exc:
            _MCP_ERRORS[name] = str(exc)[:200]
            print(f"mcp: {name} failed: {str(exc)[:120]}", flush=True)


def mcp_tool_schemas() -> list:
    return [{k: v for k, v in t.items() if k in {"type", "function"}} for t in _MCP_TOOLS]


def _call_mcp_tool(name: str, args: dict) -> str:
    for entry in _MCP_TOOLS:
        if entry["function"]["name"] == name:
            client = _MCP_CLIENTS.get(entry["server"])
            if client is None:
                return f"MCP server {entry['server']} is not connected"
            return client.call_tool(entry["tool"], args)
    return f"unknown MCP tool: {name}"


def _tool_remember(args: dict) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "no memory text given"
    item = store.add_memory(text, "agent")
    return f"remembered: {item['text'][:300]}"


_TOOL_IMPL = {
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
    "search_files": _tool_search_files,
    "list_skills": _tool_list_skills,
    "load_skill": _tool_load_skill,
    "remember": _tool_remember,
    "web_search": _tool_web_search,
    "delegate": _tool_delegate,
    "screenshot": _tool_screenshot,
    "run_command": _tool_run_command,
    "type_text": _tool_type_text,
    "press_key": _tool_press_key,
    "click": _tool_click,
    "scroll": _tool_scroll,
    "windows": _tool_windows,
    "clipboard": _tool_clipboard,
}


def _tool_write_file(args: dict) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        return "no path given"
    try:
        target = _resolve(path)
    except ValueError as exc:
        return f"refused: {exc}"
    content = str(args.get("content") or "")
    if len(content) > 200_000:
        return f"refused: content too large ({len(content)} chars, limit 200000)"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if args.get("append") else "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return f"could not write {target}: {exc}"
    verb = "appended" if args.get("append") else "wrote"
    return f"{verb} {len(content)} chars to {target}"


def _tool_find_files(args: dict) -> str:
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "no pattern given"
    try:
        root = _resolve(args.get("path") or ".")
    except ValueError as exc:
        return f"refused: {exc}"
    if not root.is_dir():
        return f"not a directory: {root}"
    limit = max(1, min(int(args.get("limit") or 50), 200))
    hits = []
    for path in root.rglob(pattern):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        hits.append((stat.st_mtime, stat.st_size, path))
        if len(hits) > 5000:
            break
    hits.sort(reverse=True)
    lines = [
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(m))}  {s:>10}  {p}"
        for m, s, p in hits[:limit]
    ]
    return "\n".join(lines) or f"no files matching {pattern!r} under {root}"


def _tool_http_get(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "no URL given"
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return "only http:// and https:// URLs are supported"
    limit = max(500, min(int(args.get("max_chars") or 8000), 40000))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Neurons)"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read(limit * 4 + 1)
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except Exception as exc:
        return f"HTTP GET failed: {str(exc)[:200]}"
    text = raw.decode("utf-8", "replace")
    if "html" in ctype.lower():
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()
    return f"HTTP {status} {ctype} — {len(text)} chars\n{text[:limit]}"


def _tool_webhook(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "no URL given"
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return "only http:// and https:// webhooks are supported"
    data = args.get("data") or args.get("payload") or {}
    if not isinstance(data, dict):
        data = {"value": data}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Neurons)"}
    extra = args.get("headers")
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(4000).decode("utf-8", "replace")
            return f"HTTP {resp.status} — {len(body)} chars\n{body[:2000]}"
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        return f"HTTP {exc.code} — {detail[:2000]}"
    except Exception as exc:
        return f"webhook failed: {str(exc)[:200]}"


def _tool_launch_app(args: dict) -> str:
    target = (args.get("target") or "").strip().strip('"')
    if not target:
        return "no target given"
    if target.startswith(("http://", "https://")):
        if not hasattr(os, "startfile"):
            return "opening URLs is Windows-only"
        os.startfile(target)
        return f"opened URL {target}"
    candidate = Path(os.path.expanduser(target))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if candidate.exists():
        if candidate.suffix.lower() in {".exe", ".bat", ".cmd", ".com"}:
            try:
                proc = subprocess.Popen([str(candidate)], cwd=str(candidate.parent))
            except OSError as exc:
                return f"could not launch {candidate}: {exc}"
            return f"launched {candidate} pid={proc.pid}"
        if not hasattr(os, "startfile"):
            return "opening documents is Windows-only"
        os.startfile(str(candidate))
        return f"opened {candidate}"
    found = shutil.which(target)
    if found:
        try:
            proc = subprocess.Popen([found], cwd=str(ROOT))
        except OSError as exc:
            return f"could not launch {found}: {exc}"
        return f"launched {found} pid={proc.pid}"
    return f"not found: {target}"


def _tool_processes(args: dict) -> str:
    if sys.platform != "win32":
        return "process listing is Windows-only"
    limit = max(1, min(int(args.get("limit") or 15), 60))
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=30,
        ).stdout
    except Exception as exc:
        return f"tasklist failed: {exc}"
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 5:
            continue
        name, pid, _session, _num, mem = parts[:5]
        try:
            kb = int(mem.replace("K", "").replace(",", "").strip())
        except ValueError:
            kb = 0
        rows.append((kb, name, pid))
    rows.sort(reverse=True)
    lines = [f"{kb / 1024:8.1f} MB  {name:<34} pid={pid}" for kb, name, pid in rows[:limit]]
    return "\n".join(lines) or "no processes"


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _POWERSTATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _tool_system_info(args: dict) -> str:
    lines = [
        f"platform: {sys.platform} {platform.machine()}",
        f"python: {sys.version.split()[0]}",
        f"cpu: {os.cpu_count()} logical cores",
    ]
    if sys.platform == "win32":
        try:
            mem = _MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                total = mem.ullTotalPhys / (1024 ** 3)
                free = mem.ullAvailPhys / (1024 ** 3)
                lines.append(f"memory: {total:.1f} GB total, {free:.1f} GB free ({mem.dwMemoryLoad}% used)")
        except Exception:
            pass
        try:
            power = _POWERSTATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)):
                percent = power.BatteryLifePercent
                batt = "unknown" if percent == 255 else f"{percent}%"
                ac = {0: "battery", 1: "plugged in", 255: "unknown"}.get(power.ACLineStatus, "unknown")
                lines.append(f"power: {ac}, battery {batt}")
        except Exception:
            pass
        try:
            uptime = ctypes.windll.kernel32.GetTickCount64() / 3_600_000
            lines.append(f"uptime: {uptime:.1f} hours")
        except Exception:
            pass
        for letter in "CDEFGH":
            drive = Path(f"{letter}:/")
            if drive.exists():
                try:
                    usage = shutil.disk_usage(drive)
                    lines.append(
                        f"disk {letter}: {usage.free / (1024 ** 3):.1f} GB free "
                        f"of {usage.total / (1024 ** 3):.1f} GB"
                    )
                except OSError:
                    pass
    return "\n".join(lines)


def _tool_volume(args: dict) -> str:
    action = (args.get("action") or "mute").lower()
    key = {"up": "volume_up", "down": "volume_down", "mute": "mute"}.get(action)
    if not key:
        return "action must be up, down or mute"
    steps = max(1, min(int(args.get("steps") or (1 if action == "mute" else 5)), 50))
    for _ in range(steps):
        _tool_press_key({"keys": key})
    return f"mute toggled x{steps}" if action == "mute" else f"volume {action} x{steps} steps"


EXTRA_TOOLS = {
    "write_file": _tool_write_file,
    "find_files": _tool_find_files,
    "http_get": _tool_http_get,
    "webhook": _tool_webhook,
    "launch_app": _tool_launch_app,
    "processes": _tool_processes,
    "system_info": _tool_system_info,
    "volume": _tool_volume,
}


def execute_tool(name: str, args: dict) -> str:
    if name.startswith("mcp__"):
        return _call_mcp_tool(name, args)
    impl = _TOOL_IMPL.get(name) or EXTRA_TOOLS.get(name)
    if impl is None:
        return f"unknown tool: {name}"
    return impl(args)


ACTION_WORDS = (
    "run", "execute", "make", "create", "write", "edit", "fix", "open", "install",
    "check", "find", "search", "scan", "read", "list", "delete", "move", "copy",
    "rename", "launch", "build", "test", "download", "upload", "click", "type",
    "screenshot", "kill", "start", "stop", "change", "set", "add", "remove",
    "update", "debug", "inspect", "explore", "clean", "organize", "save",
)


FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "Call only when the task is complete. Provide the final answer for the user; no further tools run.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
}


def _answer_or_fallback(text: str, steps: list) -> str:
    """Never end a turn with an empty bubble."""
    text = str(text or "").strip()
    if text:
        return text
    if steps:
        last = steps[-1]
        result = str(last.get("result") or "").strip()
        if result:
            return (f"(the model ended without a written answer) Last {last.get('tool', 'tool')} result:\n"
                    f"{result[:800]}")
        return f"(the model ended without a written answer) Last action: {last.get('tool', 'tool')}."
    return "(the model ended without a written answer — try again or switch models)"


def _sse_event(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def agent_stream(messages: list, model: str | None = None, max_steps: int = 40,
                 max_seconds: int = 1800,
                 provider: str | None = None):
    """Validate eagerly, then return the streaming agent iterator.

    Eager validation keeps /api/agent/stream able to answer 400 JSON before it
    commits to an SSE response.
    """
    pid, prov = resolve_provider(provider)
    model = ensure_model(pid, prov, model or prov["model"])
    return _iter_agent(personalized_messages(messages), pid, prov, model, max_steps, max_seconds)


def _iter_agent(messages, pid, prov, model, max_steps, max_seconds):
    from openai import OpenAI

    steps: list = []
    state = {"output": False}
    deadline = time.monotonic() + max_seconds
    nudged = False
    empty_finishes = 0

    def attempt(active_prov, active_pid, model_id):
        nonlocal nudged, empty_finishes
        client = OpenAI(api_key=active_prov["key"], base_url=active_prov["base"], timeout=240)
        convo = list(messages)
        resp_model = model_id
        for step in range(1, max_steps + 1):
            empty_finish = False
            yield {"type": "step", "step": step, "max_steps": max_steps}
            if time.monotonic() >= deadline:
                yield {"type": "done", "text": "(stopped: 30-minute agent budget exhausted)",
                       "model": resp_model, "provider": active_pid, "steps": steps}
                return
            stream = client.chat.completions.create(
                model=model_id, messages=convo,
                tools=TOOLS + mcp_tool_schemas() + [FINISH_TOOL],
                tool_choice="required", max_tokens=1200, stream=True,
            )
            reasoning, content = "", ""
            calls: dict = {}
            for chunk in stream:
                if getattr(chunk, "model", None):
                    resp_model = chunk.model
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                # Unknown fields (numes "reasoning", logfare "reasoning_content")
                # live in extras; model_dump() is the portable way to see them.
                fields = delta.model_dump() if hasattr(delta, "model_dump") else {}
                reason = (fields.get("reasoning") or fields.get("reasoning_content")
                          or fields.get("thinking") or "")
                if reason:
                    state["output"] = True
                    reasoning += reason
                    yield {"type": "reasoning", "delta": reason, "step": step}
                if fields.get("content"):
                    state["output"] = True
                    content += fields["content"]
                    yield {"type": "content", "delta": fields["content"], "step": step}
                for tc in fields.get("tool_calls") or []:
                    if not tc:
                        continue
                    state["output"] = True
                    idx = tc.get("index") or 0
                    slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]

            if not calls:
                latest = ""
                for message in reversed(convo):
                    if message.get("role") == "user" and isinstance(message.get("content"), str):
                        latest = message["content"].lower()
                        break
                wants_action = any(
                    re.search(r"\b" + re.escape(word) + r"\b", latest) for word in ACTION_WORDS
                )
                if wants_action and not steps and not nudged and step == 1:
                    nudged = True
                    convo.append({"role": "assistant", "content": content or ""})
                    convo.append({"role": "user", "content": (
                        "You are in agent mode on the user's PC. Do not just describe a plan "
                        "or answer from memory: take the next concrete action now with a tool, "
                        "then keep going until the task is complete."
                    )})
                    yield {"type": "nudge", "message": "no tool call — pushing the model to act"}
                    continue
                yield {"type": "done", "text": _answer_or_fallback(content, steps),
                       "model": resp_model, "provider": active_pid, "steps": steps}
                return

            ordered = [calls[i] for i in sorted(calls)]
            convo.append({
                "role": "assistant", "content": content or "",
                "tool_calls": [
                    {"id": c["id"] or f"call_{step}_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for i, c in enumerate(ordered)
                ],
            })
            for i, call in enumerate(ordered):
                name = call["name"]
                try:
                    args = json.loads(call["args"] or "{}")
                except Exception:
                    args = {}
                if name == "finish":
                    answer = str(args.get("answer") or content or "").strip()
                    if not answer and empty_finishes < 2:
                        empty_finishes += 1
                        # Calls before this one already have real tool replies;
                        # replying again would make the next request malformed.
                        for j, pending in enumerate(ordered[i:], start=i):
                            convo.append({
                                "role": "tool",
                                "tool_call_id": pending["id"] or f"call_{step}_{j}",
                                "content": ("Finish rejected: the answer was empty. Write the final answer now."
                                            if pending is call else
                                            "Not run because the finish call was rejected."),
                            })
                        convo.append({"role": "user", "content": (
                            "Your finish call had an empty answer. Write the final answer now: "
                            "what you did, what you found, and anything still unresolved. "
                            "Do not call finish with an empty answer."
                        )})
                        yield {"type": "nudge", "message": "empty finish — asking for the final answer"}
                        empty_finish = True
                        break
                    yield {"type": "done", "text": _answer_or_fallback(answer, steps),
                           "model": resp_model, "provider": active_pid, "steps": steps}
                    return
                yield {"type": "tool", "tool": name, "args": args, "step": step}
                try:
                    result = execute_tool(name, args)
                except Exception as exc:
                    result = f"ERROR: {exc}"
                steps.append({"tool": name, "args": args, "result": result[:400]})
                yield {"type": "tool_result", "tool": name,
                       "result": result[:4000], "step": step}
                convo.append({
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{step}_{i}",
                    "content": result[:8000],
                })
                # A screenshot is only useful to the model as an image part.
                if name == "screenshot" and result.startswith("SCREENSHOT:"):
                    shot_path = result.split(":", 1)[1].split("|", 1)[0]
                    try:
                        encoded = base64.b64encode(Path(shot_path).read_bytes()).decode()
                        convo.append({"role": "user", "content": [
                            {"type": "text", "text": "This is the screenshot you just captured:"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                        ]})
                    except OSError as exc:
                        convo.append({"role": "user", "content": f"screenshot could not be attached: {exc}"})
            if empty_finish:
                continue

        yield {"type": "done", "text": "(stopped: tool budget exhausted)",
               "model": resp_model, "provider": active_pid, "steps": steps}

    try:
        yield from attempt(prov, pid, model)
    except Exception as exc:
        if steps or state["output"]:
            yield {"type": "error", "error": str(exc)[:300]}
            return
        fb = fallback_provider_id(pid)
        if not fb:
            yield {"type": "error", "error": str(exc)[:300]}
            return
        fb_prov = provider_defs()[fb]
        yield {"type": "fallback", "from": pid, "to": fb, "reason": str(exc)[:160]}
        try:
            yield from attempt(fb_prov, fb, fb_prov["model"])
        except Exception as exc2:
            yield {"type": "error", "error": str(exc2)[:300]}


def run_agent(messages: list, model: str | None = None, max_steps: int = 40,
              provider: str | None = None) -> dict:
    """Small tool-calling loop: model -> tools -> model, bounded.

    If the chosen provider fails before any tool ran (dead key, no credits),
    retry once on the fallback provider with THAT provider's default model.
    Never retries after a tool ran: that would repeat real side effects.
    """
    from openai import OpenAI

    messages = personalized_messages(messages)
    steps: list = []
    empty_finishes = 0

    def attempt(prov: dict, model_id: str) -> dict:
        nonlocal empty_finishes
        client = OpenAI(api_key=prov["key"], base_url=prov["base"])
        convo = list(messages)
        resp = None
        for _ in range(max_steps):
            resp = client.chat.completions.create(
                model=model_id, messages=convo,
                tools=TOOLS + mcp_tool_schemas() + [FINISH_TOOL],
                tool_choice="required", max_tokens=1200
            )
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None)
            if not calls:
                return {"text": _answer_or_fallback(msg.content, steps),
                        "steps": steps, "model": resp.model}

            convo.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [c.model_dump() for c in calls],
            })
            for call_index, call in enumerate(calls):
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except Exception:
                    args = {}
                if name == "finish":
                    answer = str(args.get("answer") or msg.content or "").strip()
                    if not answer and empty_finishes < 2:
                        empty_finishes += 1
                        for pending in calls[call_index:]:
                            convo.append({
                                "role": "tool", "tool_call_id": pending.id,
                                "content": ("Finish rejected: the answer was empty. Write the final answer now."
                                            if pending is call else
                                            "Not run because the finish call was rejected."),
                            })
                        convo.append({"role": "user", "content": (
                            "Your finish call had an empty answer. Write the final answer now: "
                            "what you did, what you found, and anything still unresolved."
                        )})
                        break
                    return {"text": _answer_or_fallback(answer, steps),
                            "steps": steps, "model": resp.model}
                try:
                    result = execute_tool(name, args)
                except Exception as exc:
                    result = f"ERROR: {exc}"
                steps.append({"tool": name, "args": args, "result": result[:400]})
                convo.append({"role": "tool", "tool_call_id": call.id, "content": result[:8000]})
                # A screenshot is only useful to the model if it arrives as an image
                # part, not as a file path in text.
                if name == "screenshot" and result.startswith("SCREENSHOT:"):
                    shot_path = result.split(":", 1)[1].split("|", 1)[0]
                    try:
                        encoded = base64.b64encode(Path(shot_path).read_bytes()).decode()
                        convo.append({"role": "user", "content": [
                            {"type": "text", "text": "This is the screenshot you just captured:"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                        ]})
                    except OSError as exc:
                        convo.append({"role": "user", "content": f"screenshot could not be attached: {exc}"})

        return {"text": "(stopped: tool budget exhausted)", "steps": steps,
                "model": (resp.model if resp else model_id)}

    pid, prov = resolve_provider(provider)
    model = ensure_model(pid, prov, model or prov["model"])
    try:
        result = attempt(prov, model)
        return {**result, "provider": pid}
    except Exception as exc:
        fb = fallback_provider_id(pid)
        if not fb or steps:
            raise
        fb_prov = provider_defs()[fb]
        result = attempt(fb_prov, fb_prov["model"])
        return {**result, "provider": fb, "fallback_from": pid,
                "fallback_reason": str(exc)[:160]}

def toolkit_payload() -> dict:
    """Everything the drawer lists: local tools, MCP servers, installed skills."""
    skills = []
    root = _skills_root()
    if root.is_dir():
        for skill in sorted(root.iterdir()):
            md = skill / "SKILL.md"
            if not md.is_file():
                continue
            desc = ""
            try:
                for line in md.read_text(encoding="utf-8", errors="ignore").splitlines()[:20]:
                    if line.lower().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"')
                        break
            except OSError:
                pass
            skills.append({"name": skill.name, "description": desc[:220]})

    local = [
        {
            "name": t["function"]["name"],
            "description": (t["function"].get("description") or "")[:220],
        }
        for t in TOOLS
    ]

    mcp = []
    for name in _MCP_CLIENTS:
        tools = [
            {
                "name": t["function"]["name"],
                "label": t["tool"],
                "description": (t["function"].get("description") or "")[:220],
            }
            for t in _MCP_TOOLS
            if t["server"] == name
        ]
        mcp.append({"name": name, "connected": True, "tools": tools})
    for name, err in _MCP_ERRORS.items():
        if name not in _MCP_CLIENTS:
            mcp.append({"name": name, "connected": False, "error": err, "tools": []})

    return {"local": local, "mcp": mcp, "skills": skills}


WEB_ROOT = Path(__file__).resolve().parent / "web"




class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second process silently bind the same
    # port, so keep it off and detect a busy port instead of hijacking it.
    allow_reuse_address = False
    daemon_threads = True


class ServerV6(Server):
    """Loopback IPv6, so http://localhost works when it resolves to ::1."""

    address_family = socket.AF_INET6


# Cached model catalogue per provider: the dropdown is instant, refreshes in background.
_MODELS_CACHE: dict = {}
_MODELS_TTL = 120.0
_models_lock = threading.Lock()


def _is_free_model(entry: dict) -> bool:
    """True when a catalogue row costs nothing: :free suffix or all-zero pricing."""
    if str(entry.get("id", "")).endswith(":free"):
        return True
    pricing = entry.get("pricing") or {}
    values = []
    for key in ("prompt", "completion", "request", "input", "output"):
        if key in pricing:
            try:
                values.append(float(pricing[key]))
            except (TypeError, ValueError):
                return False
    return bool(values) and all(v == 0 for v in values)


def get_models(force: bool = False, provider_id: str | None = None) -> dict:
    pid, prov = resolve_provider(provider_id)
    now = time.time()
    with _models_lock:
        entry = _MODELS_CACHE.get(pid)
        fresh = entry is not None and (now - entry["at"]) < _MODELS_TTL
        cached = entry["data"] if entry else None
    if fresh and not force:
        return cached

    req = urllib.request.Request(
        prov["base"] + "/models",
        headers={"Authorization": f"Bearer {prov['key']}"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())

    with _models_lock:
        _MODELS_CACHE[pid] = {"data": data, "at": time.time()}
    return data


def prewarm_models() -> None:
    try:
        pid, prov = resolve_provider()
        get_models(provider_id=pid)
        print(f"model catalogue cached ({prov['label']})", flush=True)
    except Exception as exc:
        print(f"could not preload models: {str(exc)[:120]}", flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "Neurons/1.0"
    # Without this, small SSE frames sit in Nagle's buffer (~40ms each).
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path in ("/", "/index.html"):
            try:
                body = (WEB_ROOT / "index.html").read_bytes()
            except OSError as exc:
                self._json(500, {"error": f"UI unavailable: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/assets/"):
            name = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            asset_types = {
                "styles.css": "text/css; charset=utf-8",
                "app-extra.css": "text/css; charset=utf-8",
                "app.js": "text/javascript; charset=utf-8",
                "app-shell.js": "text/javascript; charset=utf-8",
            }
            if name not in asset_types:
                self._json(404, {"error": "not found"})
                return
            try:
                body = (WEB_ROOT / name).read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", asset_types[name])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/health"):
            self._json(200, {"ok": True, "app": "neurons", "version": "2.0"})
            return
        if self.path.startswith("/api/state"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (qs.get("conversation") or [""])[0] or None
            self._json(200, store.get_state(cid))
            return
        if self.path.startswith("/api/conversations"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (qs.get("id") or [""])[0] or None
            if not cid:
                self._json(200, {"conversations": store.list_conversations()})
                return
            conv = store.get_conversation(cid)
            if not conv:
                self._json(404, {"error": "conversation not found"})
                return
            self._json(200, conv)
            return
        if self.path.startswith("/api/models"):
            self._proxy_models()
            return
        if self.path.startswith("/api/mcp"):
            self._json(200, {
                "started": _mcp_started,
                "servers": {
                    name: {
                        "connected": True,
                        "tools": [t["tool"] for t in _MCP_TOOLS if t["server"] == name],
                    }
                    for name in _MCP_CLIENTS
                },
                "errors": _MCP_ERRORS,
            })
            return
        if self.path.startswith("/api/providers"):
            self._json(200, {
                "providers": [
                    {"id": pid, "label": p["label"], "base_url": p["base"], "model": p["model"]}
                    for pid, p in provider_defs().items()
                ],
                "default": default_provider_id(),
                "fallback": fallback_provider_id(default_provider_id()),
            })
            return
        if self.path.startswith("/api/toolkit"):
            self._json(200, toolkit_payload())
            return
        if self.path.startswith("/api/screenshot"):
            self._screenshot()
            return
        self._json(404, {"error": "not found"})

    def _screenshot(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = (qs.get("path") or [""])[0]
        try:
            path = Path(raw).resolve()
            root = Path(tempfile.gettempdir()).resolve()
            ok = root in path.parents and path.suffix.lower() == ".png" and path.is_file()
        except OSError:
            ok = False
        if not ok:
            self._json(404, {"error": "not found"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _run_one_tool(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        name = str(payload.get("name") or "")
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            self._json(400, {"error": "args must be a JSON object"})
            return
        mcp_names = {t["function"]["name"] for t in _MCP_TOOLS}
        local_names = {t["function"]["name"] for t in TOOLS}
        if name not in mcp_names and name not in local_names:
            self._json(404, {"error": f"unknown tool: {name}"})
            return
        try:
            result = _call_mcp_tool(name, args) if name in mcp_names else execute_tool(name, args)
        except Exception as exc:
            self._json(502, {"error": str(exc)[:300]})
            return
        self._json(200, {"result": str(result)[:20000]})

    def _proxy_models(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        provider_id = (qs.get("provider") or [""])[0] or None
        try:
            pid, prov = resolve_provider(provider_id)
            data = get_models(force="refresh=1" in self.path, provider_id=pid)
            raw = [m for m in data.get("data", []) if m.get("id")]
            # Numes bills per request against a balance; only the zero-priced
            # models work on an unfunded account, so never list the others.
            if pid == "numes":
                raw = [m for m in raw if _is_free_model(m)]
            # Slim payload: the browser only needs id + label, not the whole
            # provider metadata blob (~60KB -> ~2KB).
            models = [
                {"id": m["id"], "name": m.get("name") or m["id"]}
                for m in raw
            ]
            self._json(200, {
                "data": models,
                "default_model": prov["model"],
                "provider": pid,
                "provider_label": prov["label"],
                "cached": True,
            })
        except Exception as exc:
            self._json(502, {"error": str(exc)[:300]})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "JSON object required"})
            return None
        return payload

    def do_POST(self):
        if self.path == "/api/profile":
            payload = self._read_json()
            if payload is None:
                return
            try:
                self._json(200, {"profile": store.update_profile(payload)})
            except Exception as exc:
                self._json(400, {"error": str(exc)[:300]})
            return
        if self.path == "/api/memory":
            payload = self._read_json()
            if payload is None:
                return
            try:
                self._json(200, {"memory": store.add_memory(payload.get("text"), payload.get("source") or "user")})
            except Exception as exc:
                self._json(400, {"error": str(exc)[:300]})
            return
        if self.path == "/api/memory/delete":
            payload = self._read_json()
            if payload is None:
                return
            try:
                removed = store.delete_memory(int(payload.get("id")))
                self._json(200 if removed else 404, {"deleted": removed})
            except Exception as exc:
                self._json(400, {"error": str(exc)[:300]})
            return
        if self.path == "/api/conversations":
            payload = self._read_json()
            if payload is None:
                return
            self._json(200, {"conversation": store.create_conversation(payload.get("title") or "New chat")})
            return
        if self.path == "/api/conversations/delete":
            payload = self._read_json()
            if payload is None:
                return
            removed = store.delete_conversation(payload.get("id"))
            self._json(200 if removed else 404, {"deleted": removed})
            return
        if self.path == "/api/messages":
            payload = self._read_json()
            if payload is None:
                return
            cid = str(payload.get("conversation_id") or "")
            if not store.get_conversation(cid):
                self._json(404, {"error": "conversation not found"})
                return
            try:
                message = store.append_message(cid, payload.get("role"), payload.get("content"), payload.get("meta"))
                self._json(200, {"message": message, "conversation": store.get_conversation(cid)})
            except Exception as exc:
                self._json(400, {"error": str(exc)[:300]})
            return
        if self.path == "/api/skills/install":
            self._install_skills()
            return
        if self.path == "/api/tool":
            self._run_one_tool()
            return
        if self.path == "/api/agent/stream":
            self._agent_stream()
            return
        if self.path == "/api/agent":
            self._agent()
            return
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        messages = payload.get("messages") or []
        if not messages:
            messages = [{"role": "user", "content": "Hello"}]
        messages = personalized_messages(messages)
        try:
            used_id, used = resolve_provider(payload.get("provider"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        model = payload.get("model") or used["model"]
        try:
            model = ensure_model(used_id, used, model)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        body = json.dumps({
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": 1500,
        }).encode()

        req = urllib.request.Request(
            used["base"] + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {used['key']}",
            },
            method="POST",
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Must close: an SSE body has no Content-Length, so without this the
        # client waits for EOF forever after the last frame (or an error).
        self.send_header("Connection", "close")
        self.send_header("X-Neurons-Provider", used_id)
        self.end_headers()
        self.close_connection = True

        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                for raw in resp:
                    if not raw.strip():
                        continue
                    try:
                        self.wfile.write(raw)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            frame = f"data: {json.dumps({'error': f'HTTP {exc.code}: {detail}'})}\n\n"
            self.wfile.write(frame.encode())
            self.wfile.flush()
        except Exception as exc:
            frame = f"data: {json.dumps({'error': str(exc)[:300]})}\n\n"
            try:
                self.wfile.write(frame.encode())
                self.wfile.flush()
            except Exception:
                pass


    def _install_skills(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        try:
            result = install_all_skills(overwrite=bool(payload.get("overwrite")))
        except Exception as exc:
            self._json(500, {"error": str(exc)[:300]})
            return
        self._json(200, result)

    def _agent_stream(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        messages = payload.get("messages") or []
        if not messages:
            self._json(400, {"error": "messages required"})
            return
        try:
            events = agent_stream(messages, payload.get("model"),
                                  provider=payload.get("provider") or None)
        except ValueError as exc:
            self._json(400, {"error": str(exc)[:300]})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for event in events:
                self.wfile.write(_sse_event(event))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self.wfile.write(_sse_event({"type": "error", "error": str(exc)[:300]}))
                self.wfile.flush()
            except Exception:
                pass

    def _agent(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        messages = payload.get("messages") or []
        if not messages:
            self._json(400, {"error": "messages required"})
            return

        try:
            result = run_agent(messages, payload.get("model"),
                               provider=payload.get("provider") or None)
            self._json(200, result)
        except ValueError as exc:
            self._json(400, {"error": str(exc)[:300]})
        except Exception as exc:
            self._json(502, {"error": str(exc)[:300]})


def bind_server(host: str, port: int, tries: int = 10):
    """Bind the first free port, so a second window never dies on EADDRINUSE."""
    last_error = None
    for candidate in range(port, port + tries):
        try:
            return Server((host, candidate), Handler), candidate
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"no free port in {port}-{port + tries - 1}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Neurons local agent app")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workspace", default=None,
                        help="root for file and shell tools (default: the app folder)")
    parser.add_argument("--data", default=None,
                        help="data folder for the SQLite memory database (default: ./data)")
    parser.add_argument("--open", action="store_true", help="open the UI in your browser")
    parser.add_argument("--base-url", default=None, help="override LOGFARE_BASE_URL (debugging)")
    parser.add_argument("--allow-screen", dest="screen", action="store_true",
                        help="capture the screen (already on by default; kept for old scripts)")
    parser.add_argument("--no-screen", dest="screen", action="store_false",
                        help="disable screen capture")
    parser.add_argument("--no-shell", dest="shell", action="store_false",
                        help="disable the run_command tool (it is on by default)")
    parser.set_defaults(screen=True, shell=True)
    args = parser.parse_args()

    global ALLOW_SCREEN, ALLOW_SHELL, WORKSPACE_ROOT, ROOT
    ALLOW_SCREEN = bool(args.screen)
    ALLOW_SHELL = bool(args.shell)

    if args.workspace:
        WORKSPACE_ROOT = Path(args.workspace).expanduser().resolve()
        ROOT = WORKSPACE_ROOT

    data_dir = Path(args.data).expanduser() if args.data else Path(env_value("NEURONS_DATA") or APP_ROOT / "data")
    store.init_db(data_dir / "neurons.db")

    if args.base_url:
        global BASE_URL
        BASE_URL = args.base_url.rstrip("/")

    if not API_KEY:
        print("no API key: set NEURONS_API_KEY (or LOGFARE_API_KEY)")
        return 1

    server, bound_port = bind_server(args.host, args.port)
    if bound_port != args.port:
        print(f"port {args.port} was busy, using {bound_port}", flush=True)

    url = f"http://{args.host}:{bound_port}"
    print(f"Neurons  ->  {url}", flush=True)
    print(f"memory db: {store.DB_PATH}", flush=True)
    print(f"model default: {DEFAULT_MODEL}   (Ctrl+C to stop)", flush=True)

    # Browsers may resolve "localhost" to ::1 first; answer there too.
    try:
        v6 = ServerV6(("::1", bound_port), Handler)
        threading.Thread(target=v6.serve_forever, daemon=True).start()
        print(f"also on http://localhost:{bound_port} (IPv6 loopback)", flush=True)
    except OSError as exc:
        print(f"IPv6 loopback unavailable: {exc}", flush=True)

    # Warm the cache in the background so the first page load is instant.
    threading.Thread(target=prewarm_models, daemon=True).start()
    # MCP servers are subprocesses: never block the UI waiting for them.
    threading.Thread(target=mcp_connect_all, daemon=True).start()

    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
