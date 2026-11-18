"""Local integration broker for Neurons.

Credentials are stored server-side in ``data/plugins.json`` with user-only
permissions and are never returned to the browser. The agent gets a single
``plugin_call`` tool so a connected service can be used without teaching the
model a new tool for every API.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
MAX_RESPONSE = 180_000
DEFAULT_TIMEOUT = 30


def _field(key: str, label: str, *, secret: bool = True, required: bool = True,
           placeholder: str = "", help_text: str = "") -> dict:
    return {
        "key": key, "label": label, "secret": secret, "required": required,
        "placeholder": placeholder, "help": help_text,
    }


CATALOG: list[dict] = [
    {
        "id": "github", "name": "GitHub", "category": "Code",
        "glyph": "GH", "color": "#8b5cf6",
        "description": "Repositories, issues, pull requests, releases and Actions.",
        "docs": "https://docs.github.com/en/rest",
        "base": "https://api.github.com", "auth": "bearer",
        "fields": [_field("token", "Personal access token", placeholder="github_pat_…")],
        "test": {"method": "GET", "path": "/user"},
    },
    {
        "id": "gmail", "name": "Gmail", "category": "Communication",
        "glyph": "GM", "color": "#ea4335",
        "description": "Search and read mail, labels and threads through the Gmail API.",
        "docs": "https://developers.google.com/gmail/api/reference/rest",
        "base": "https://gmail.googleapis.com", "auth": "bearer",
        "fields": [_field("token", "OAuth access token", placeholder="ya29.…")],
        "test": {"method": "GET", "path": "/gmail/v1/users/me/profile"},
    },
    {
        "id": "google-calendar", "name": "Google Calendar", "category": "Productivity",
        "glyph": "GC", "color": "#4285f4",
        "description": "List calendars and events, and create new events.",
        "docs": "https://developers.google.com/calendar/api/v3/reference",
        "base": "https://www.googleapis.com/calendar/v3", "auth": "bearer",
        "fields": [_field("token", "OAuth access token", placeholder="ya29.…")],
        "test": {"method": "GET", "path": "/users/me/calendarList"},
    },
    {
        "id": "google-drive", "name": "Google Drive", "category": "Productivity",
        "glyph": "GD", "color": "#fbbc04",
        "description": "Search files and folders and inspect file metadata.",
        "docs": "https://developers.google.com/drive/api/reference/rest/v3",
        "base": "https://www.googleapis.com/drive/v3", "auth": "bearer",
        "fields": [_field("token", "OAuth access token", placeholder="ya29.…")],
        "test": {"method": "GET", "path": "/about?fields=user"},
    },
    {
        "id": "notion", "name": "Notion", "category": "Knowledge",
        "glyph": "NO", "color": "#e5e7eb",
        "description": "Search pages and databases, read blocks and create pages.",
        "docs": "https://developers.notion.com/reference/intro",
        "base": "https://api.notion.com/v1", "auth": "bearer",
        "headers": {"Notion-Version": "2022-06-28"},
        "fields": [_field("token", "Internal integration token", placeholder="ntn_…")],
        "test": {"method": "GET", "path": "/users/me"},
    },
    {
        "id": "outlook", "name": "Outlook", "category": "Communication",
        "glyph": "OU", "color": "#0f6cbd",
        "description": "Read Microsoft 365 mail and calendar data through Microsoft Graph.",
        "docs": "https://learn.microsoft.com/graph/api/overview",
        "base": "https://graph.microsoft.com/v1.0", "auth": "bearer",
        "fields": [_field("token", "Microsoft Graph access token", placeholder="eyJ0eXAi…")],
        "test": {"method": "GET", "path": "/me"},
    },
    {
        "id": "sigma", "name": "Sigma", "category": "Analytics",
        "glyph": "SI", "color": "#22d3ee",
        "description": "Query Sigma workbooks, datasets and members with client credentials.",
        "docs": "https://help.sigmacomputing.com/reference",
        "base": "https://api.sigmacomputing.com", "auth": "sigma",
        "fields": [
            _field("client_id", "Client ID", secret=False, placeholder="Sigma client ID"),
            _field("client_secret", "Client secret", placeholder="Sigma client secret"),
            _field("base_url", "API base URL", secret=False, required=False,
                   placeholder="https://api.sigmacomputing.com"),
        ],
        "test": {"method": "GET", "path": "/v2/members?limit=1"},
    },
    {
        "id": "slack", "name": "Slack", "category": "Communication",
        "glyph": "SL", "color": "#36c5f0",
        "description": "Read channels and post messages with a bot or user token.",
        "docs": "https://api.slack.com/methods",
        "base": "https://slack.com/api", "auth": "bearer",
        "fields": [_field("token", "OAuth token", placeholder="xoxb-…")],
        "test": {"method": "POST", "path": "/auth.test"},
    },
    {
        "id": "discord", "name": "Discord", "category": "Communication",
        "glyph": "DI", "color": "#5865f2",
        "description": "Inspect guilds and channels and send bot messages.",
        "docs": "https://discord.com/developers/docs/intro",
        "base": "https://discord.com/api/v10", "auth": "bearer",
        "fields": [_field("token", "Bot token", placeholder="Bot …")],
        "test": {"method": "GET", "path": "/users/@me"},
    },
    {
        "id": "linear", "name": "Linear", "category": "Code",
        "glyph": "LI", "color": "#5e6ad2",
        "description": "Search issues, teams and projects through Linear's GraphQL API.",
        "docs": "https://developers.linear.app/docs/graphql/working-with-the-graphql-api",
        "base": "https://api.linear.app", "auth": "bearer",
        "fields": [_field("token", "Personal API key", placeholder="lin_api_…")],
        "test": {"method": "GET", "path": "/graphql?query=%7Bviewer%7Bid%7D%7D"},
    },
    {
        "id": "jira", "name": "Jira", "category": "Code",
        "glyph": "JI", "color": "#2684ff",
        "description": "Search issues and projects on your Jira Cloud site.",
        "docs": "https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
        "base": "", "base_field": "site_url", "auth": "basic",
        "fields": [
            _field("site_url", "Jira site URL", secret=False,
                   placeholder="https://your-team.atlassian.net"),
            _field("email", "Account email", secret=False, placeholder="you@example.com"),
            _field("token", "API token", placeholder="Jira API token"),
        ],
        "test": {"method": "GET", "path": "/rest/api/3/myself"},
    },
    {
        "id": "airtable", "name": "Airtable", "category": "Data",
        "glyph": "AI", "color": "#fbbf24",
        "description": "Read and create records in Airtable bases.",
        "docs": "https://airtable.com/developers/web/api/introduction",
        "base": "https://api.airtable.com/v0", "auth": "bearer",
        "fields": [_field("token", "Personal access token", placeholder="pat…")],
    },
    {
        "id": "stripe", "name": "Stripe", "category": "Commerce",
        "glyph": "ST", "color": "#635bff",
        "description": "Inspect customers, payments, subscriptions and products.",
        "docs": "https://docs.stripe.com/api",
        "base": "https://api.stripe.com/v1", "auth": "bearer",
        "fields": [_field("token", "Secret key", placeholder="sk_live_…")],
        "test": {"method": "GET", "path": "/customers?limit=1"},
    },
    {
        "id": "hubspot", "name": "HubSpot", "category": "CRM",
        "glyph": "HU", "color": "#ff7a59",
        "description": "Read contacts, companies and deals from HubSpot CRM.",
        "docs": "https://developers.hubspot.com/docs/api/overview",
        "base": "https://api.hubapi.com", "auth": "bearer",
        "fields": [_field("token", "Private app access token", placeholder="pat-…")],
    },
    {
        "id": "salesforce", "name": "Salesforce", "category": "CRM",
        "glyph": "SF", "color": "#00a1e0",
        "description": "Query Salesforce records through the REST API.",
        "docs": "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
        "base": "", "base_field": "instance_url", "auth": "bearer",
        "fields": [
            _field("instance_url", "Instance URL", secret=False,
                   placeholder="https://your-domain.my.salesforce.com"),
            _field("token", "Access token", placeholder="00D…"),
        ],
        "test": {"method": "GET", "path": "/services/data/v62.0/sobjects"},
    },
    {
        "id": "asana", "name": "Asana", "category": "Productivity",
        "glyph": "AS", "color": "#f06a6a",
        "description": "List projects and tasks and create work in Asana.",
        "docs": "https://developers.asana.com/reference/rest-api-reference",
        "base": "https://app.asana.com/api/1.0", "auth": "bearer",
        "fields": [_field("token", "Personal access token", placeholder="1/…")],
        "test": {"method": "GET", "path": "/users/me"},
    },
    {
        "id": "dropbox", "name": "Dropbox", "category": "Storage",
        "glyph": "DR", "color": "#0061ff",
        "description": "List folders and inspect files in Dropbox.",
        "docs": "https://www.dropbox.com/developers/documentation/http/documentation",
        "base": "https://api.dropboxapi.com/2", "auth": "bearer",
        "fields": [_field("token", "Access token", placeholder="sl.…")],
        "test": {"method": "POST", "path": "/users/get_current_account"},
    },
    {
        "id": "supabase", "name": "Supabase", "category": "Data",
        "glyph": "SU", "color": "#3ecf8e",
        "description": "Read tables through your Supabase project's REST endpoint.",
        "docs": "https://supabase.com/docs/guides/api",
        "base": "", "base_field": "project_url", "auth": "bearer",
        "fields": [
            _field("project_url", "Project URL", secret=False,
                   placeholder="https://xxxx.supabase.co"),
            _field("token", "Service role key", placeholder="eyJ…"),
        ],
    },
    {
        "id": "sentry", "name": "Sentry", "category": "Code",
        "glyph": "SE", "color": "#e1567c",
        "description": "Inspect issues, events and projects in Sentry.",
        "docs": "https://docs.sentry.io/api/",
        "base": "https://sentry.io/api/0", "auth": "bearer",
        "fields": [_field("token", "Auth token", placeholder="sntrys_…")],
        "test": {"method": "GET", "path": "/projects/"},
    },
    {
        "id": "pagerduty", "name": "PagerDuty", "category": "Operations",
        "glyph": "PD", "color": "#06ac38",
        "description": "Inspect incidents, services and on-call schedules.",
        "docs": "https://developer.pagerduty.com/api-reference/",
        "base": "https://api.pagerduty.com", "auth": "bearer",
        "fields": [_field("token", "REST API key", placeholder="u+…")],
        "test": {"method": "GET", "path": "/users/me"},
    },
    {
        "id": "google-sheets", "name": "Google Sheets", "category": "Data",
        "glyph": "GS", "color": "#34a853",
        "description": "Read and write spreadsheet values through the Sheets API.",
        "docs": "https://developers.google.com/sheets/api/reference/rest",
        "base": "https://sheets.googleapis.com/v4", "auth": "bearer",
        "fields": [_field("token", "OAuth access token", placeholder="ya29.…")],
    },
]


BY_ID = {entry["id"]: entry for entry in CATALOG}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public_entry(entry: dict, connection: dict | None) -> dict:
    item = {key: value for key, value in entry.items() if key not in {"fields", "test"}}
    item["fields"] = [
        {key: value for key, value in field.items() if key != "value"}
        for field in entry.get("fields", [])
    ]
    item["connected"] = bool(connection)
    item["connected_at"] = (connection or {}).get("connected_at", "")
    item["account"] = (connection or {}).get("account", "")
    return item


class PluginError(ValueError):
    pass


class PluginManager:
    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._sigma_tokens: dict[str, tuple[str, float]] = {}
        default = Path(os.environ.get("NEURONS_DATA") or APP_ROOT / "data") / "plugins.json"
        self.path = Path(path) if path else default

    def configure(self, data_dir: str | Path) -> None:
        with self._lock:
            self.path = Path(data_dir) / "plugins.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self.path)

    def catalog(self) -> dict:
        with self._lock:
            saved = self._load()
            connections = saved.get("connections") if isinstance(saved.get("connections"), dict) else {}
            items = [_public_entry(entry, connections.get(entry["id"])) for entry in CATALOG]
            return {
                "plugins": items,
                "connected": [item["id"] for item in items if item["connected"]],
            }

    def connect(self, plugin_id: str, fields: dict) -> dict:
        entry = BY_ID.get(plugin_id)
        if not entry:
            raise PluginError("unknown plugin")
        if not isinstance(fields, dict):
            raise PluginError("fields must be an object")
        allowed = {field["key"] for field in entry.get("fields", [])}
        values = {key: str(fields[key]).strip() for key in allowed if key in fields and fields[key] is not None}
        for field in entry.get("fields", []):
            if field.get("required", True) and not values.get(field["key"]):
                raise PluginError(f"{field['label']} is required")
        base = values.get(entry.get("base_field", "")) or entry.get("base") or ""
        if base and not base.startswith("https://"):
            raise PluginError("the service URL must start with https://")
        values = {key: value[:8000] for key, value in values.items()}
        with self._lock:
            saved = self._load()
            connections = saved.setdefault("connections", {})
            connections[plugin_id] = {
                "fields": values,
                "account": values.get("email") or values.get("site_url") or values.get("instance_url") or values.get("project_url") or "",
                "connected_at": _now(),
            }
            self._save(saved)
            return self.catalog()

    def disconnect(self, plugin_id: str) -> dict:
        with self._lock:
            saved = self._load()
            connections = saved.get("connections")
            if isinstance(connections, dict):
                connections.pop(plugin_id, None)
                self._save(saved)
            return self.catalog()

    def configured(self, plugin_id: str) -> bool:
        with self._lock:
            saved = self._load()
            return plugin_id in (saved.get("connections") or {})

    def _connection(self, plugin_id: str) -> tuple[dict, dict]:
        entry = BY_ID.get(plugin_id)
        if not entry:
            raise PluginError("unknown plugin")
        with self._lock:
            saved = self._load()
            connection = (saved.get("connections") or {}).get(plugin_id)
        if not connection:
            raise PluginError(f"{entry['name']} is not connected")
        return entry, connection

    def _sigma_token(self, entry: dict, values: dict) -> str:
        base = (values.get("base_url") or entry.get("base") or "").rstrip("/")
        cache_key = base + "|" + values.get("client_id", "")
        with self._lock:
            cached = self._sigma_tokens.get(cache_key)
            if cached and cached[1] > time.time():
                return cached[0]
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": values.get("client_id", ""),
            "client_secret": values.get("client_secret", ""),
        }).encode()
        req = urllib.request.Request(
            base + "/v2/auth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Neurons/2.3"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as response:
            payload = json.loads(response.read(MAX_RESPONSE).decode("utf-8", "replace"))
        token = str(payload.get("access_token") or "")
        if not token:
            raise PluginError("Sigma did not return an access token")
        expires = max(60, int(payload.get("expires_in") or 3600) - 60)
        with self._lock:
            self._sigma_tokens[cache_key] = (token, time.time() + expires)
        return token

    def _auth_headers(self, entry: dict, values: dict) -> dict:
        auth = entry.get("auth")
        token = values.get("token", "")
        if auth == "sigma":
            token = self._sigma_token(entry, values)
        if auth == "basic":
            raw = (values.get("email", "") + ":" + token).encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        if auth in ("bearer", "sigma"):
            return {"Authorization": "Bearer " + token}
        return {}

    def call(self, plugin_id: str, method: str, path: str, query: dict | None = None,
             body: dict | None = None) -> dict:
        entry, connection = self._connection(plugin_id)
        values = connection.get("fields") or {}
        base = (values.get(entry.get("base_field", "")) or entry.get("base") or "").rstrip("/")
        if not base.startswith("https://"):
            raise PluginError("plugin base URL must use https")
        path = str(path or "/").strip()
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise PluginError("path must be relative to the service base URL")
        if isinstance(query, dict) and query:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(
                {str(k): str(v) for k, v in query.items()}
            )
        method = str(method or "GET").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise PluginError("unsupported method")
        data = None
        headers = {"User-Agent": "Neurons/2.3", "Accept": "application/json", **entry.get("headers", {})}
        headers.update(self._auth_headers(entry, values))
        if body is not None and method not in {"GET", "HEAD"}:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        host = urllib.parse.urlparse(base).netloc

        class SameHostRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if urllib.parse.urlparse(newurl).netloc != host:
                    raise PluginError("refusing cross-service redirect")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(SameHostRedirect())
        try:
            with opener.open(req, timeout=DEFAULT_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE + 1)
                status = response.status
                ctype = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read(MAX_RESPONSE).decode("utf-8", "replace")
            return {"status": exc.code, "service": plugin_id, "body": detail[:MAX_RESPONSE]}
        try:
            parsed = json.loads(raw.decode("utf-8", "replace")) if "json" in ctype.lower() else None
        except ValueError:
            parsed = None
        text = json.dumps(parsed, indent=2) if parsed is not None else raw.decode("utf-8", "replace")
        return {"status": status, "service": plugin_id, "body": text[:MAX_RESPONSE]}

    def call_text(self, args: dict) -> str:
        plugin_id = str(args.get("service") or "").strip()
        if not plugin_id:
            return "no connected service given"
        try:
            result = self.call(
                plugin_id,
                str(args.get("method") or "GET"),
                str(args.get("path") or "/"),
                args.get("query") if isinstance(args.get("query"), dict) else None,
                args.get("body") if isinstance(args.get("body"), dict) else None,
            )
        except Exception as exc:
            return f"plugin error: {str(exc)[:300]}"
        return f"HTTP {result['status']} · {plugin_id}\n{result['body']}"

    def test(self, plugin_id: str) -> dict:
        entry, _ = self._connection(plugin_id)
        test = entry.get("test")
        if not test:
            return {"ok": True, "status": 0, "body": "connected (no test endpoint configured)"}
        result = self.call(plugin_id, test.get("method", "GET"), test.get("path", "/"))
        return {**result, "ok": 200 <= int(result.get("status") or 0) < 400}

    def connected_summary(self) -> str:
        payload = self.catalog()
        return ", ".join(payload["connected"]) or "none"


manager = PluginManager()

PLUGIN_TOOL = {
    "type": "function",
    "function": {
        "name": "plugin_call",
        "description": "Call a connected integration (GitHub, Notion, Gmail, Slack, Sigma, etc.). Use GET to read and POST/PATCH to change data. Credentials stay on the server.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Connected plugin id, e.g. github or notion"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": "Path relative to the service base, e.g. /user/repos"},
                "query": {"type": "object", "description": "Optional query parameters"},
                "body": {"type": "object", "description": "Optional JSON request body"},
            },
            "required": ["service", "method", "path"],
        },
    },
}


def plugin_tool_schema() -> dict:
    return PLUGIN_TOOL
