# Neurons

A local-first AI agent with a real app shell. It streams reasoning and tool
calls, keeps a persistent profile and memory, saves conversations, and can use
your files, shell, browser, screen and desktop through explicit tools.

The API key stays on the server. The browser never receives it.

## What is in the app

- **Persistent memory** — profile, durable facts and every chat are stored in
  SQLite at `data/neurons.db` (configurable). A `remember` tool lets the model
  save lasting preferences; you can add or remove facts in the UI.
- **Personalization** — your name, background/about, and response style are
  injected into every new conversation. The model can also remember details
  from earlier chats.
- **Saved conversation history** — chats appear in the sidebar and reload after
  a browser restart. Delete individual chats from the sidebar.
- **Switchable UI** — `graphite`, `paper` and `terminal` themes. The choice is
  stored locally and on the server profile.
- **Resilient streams** — transient provider failures retry automatically, an
  empty `finish` gets a final prose pass, and failed bubbles offer **retry**.
- **Agent workspace** — files, shell, web, desktop control, skills, MCP servers,
  subagents and tool traces. Agent mode is on by default.
- **Queue controls** and stop/cancel from the existing harness.

## Quick start

```bash
cd neurons
py -3.12 -m venv .venv
source .venv/Scripts/activate     # git-bash on Windows
pip install -r requirements.txt
cp .env.example .env              # fill in one provider
py server.py --open
```

Open <http://127.0.0.1:8770>. If the app is started from this folder, the file
and shell tools default to this folder as their workspace. To point them at a
project, pass `--workspace`:

```bash
py server.py --open --workspace "C:/Users/Cameron/spirulina"
```

On Windows, `py -3.12` is recommended because the machine's bare `python` can
be older than the syntax used here.

### Configuration

The server loads, in order:

1. `neurons/.env`
2. the file at `NEURONS_ENV_FILE` (if set)
3. `../.env`
4. legacy local installs at `../orrtechnologies/.env.local`
5. the process environment

The first configured value wins, so an explicitly set app `.env` is not
silently shadowed by an old shell export.

| Variable | Purpose |
|---|---|
| `NUMES_API_KEY`, `NUMES_BASE_URL`, `NUMES_MODEL` | Numes provider |
| `LOGFARE_API_KEY`, `LOGFARE_BASE_URL`, `LOGFARE_MODEL` | Logfare provider |
| `NEURONS_API_KEY`, `NEURONS_BASE_URL`, `NEURONS_MODEL` | Any extra OpenAI-compatible provider |
| `NEURONS_PROVIDER` | Provider selected at startup |
| `NEURONS_FALLBACK` | Retry provider if the first fails before a tool runs |
| `NEURONS_WORKSPACE` | Root for file and shell tools (defaults to the app folder) |
| `NEURONS_DATA` | Data directory for `neurons.db` (defaults to `./data`) |
| `NEURONS_ENV_FILE` | Extra `.env` file to load |
| `NEURONS_SKILLS` | Skills directory (defaults to `./.agents/skills`) |
| `NEURONS_MCP_CONFIG` | MCP server config (defaults to `./.mcp.json`) |

Never commit `.env` or `data/`: both are ignored.

## Memory model

`store.py` owns one SQLite database:

| Table | Contents |
|---|---|
| `profile` | name, about, response style, theme |
| `memories` | durable facts with source and timestamp |
| `conversations` | chat titles and timestamps |
| `messages` | user/assistant turns per conversation |

The server prepends the profile and memories to the model's system context on
every request. The client never supplies trusted memory; it only displays and
edits what the server returns. A `remember` call from the agent appends a fact,
and duplicate facts are collapsed case-insensitively.

Memory is not a replacement for reading the actual files or checking a command
result. The agent is still instructed to verify facts before reporting them.

## Provider reliability

Agent requests retry transient connection drops, timeouts, rate limits and
5xx responses up to three times. If a provider fails before any tool runs, the
fallback provider is tried. If tools already ran and the provider then drops,
Neurons shows the last completed tool result rather than re-running side
effects.

When a model calls `finish` with an empty answer twice, Neurons makes one final
request with tools removed and asks for plain prose. The UI also adds a
**retry** button to a failed bubble. This avoids the old dead-end outcome where
a task had completed but the chat showed only a network error.

## UI

The app shell is in `web/`:

- `index.html` — sidebar, chat, profile/memory drawer, toolkit, themes
- `styles.css` — base chat and toolkit styles
- `app-extra.css` — app shell, personalization, theme and responsive styles
- `app.js` — streaming agent/chat, queue and toolkit
- `app-shell.js` — saved chats, profile, memory, themes and persistence

Themes are applied with `data-theme` on `<html>` and use the shared CSS custom
properties. On screens under 760 px the sidebar becomes an off-canvas drawer.

## API

New app endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | profile, memories, saved chats and the active conversation |
| `POST /api/profile` | partial profile update: `name`, `about`, `style`, `theme` |
| `POST /api/memory` | add `{ "text": "..." }` |
| `POST /api/memory/delete` | delete `{ "id": 1 }` |
| `POST /api/conversations` | create a chat `{ "title": "..." }` |
| `GET /api/conversations?id=…` | load one chat and its messages |
| `POST /api/conversations/delete` | delete `{ "id": "..." }` |
| `POST /api/messages` | append `{ "conversation_id", "role", "content" }` |
| `GET /api/health` | liveness check |

Existing endpoints remain: `/api/models`, `/api/providers`, `/api/toolkit`,
`/api/tool`, `/api/skills/install`, `/api/chat`, `/api/agent/stream`,
`/api/agent`, and `/api/screenshot`.

## Tests

```bash
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

The store tests use a temporary database and cover profile validation, memory
deduplication/deletion, conversation ordering and cascade deletion.

## Security

This is a local-first agent with **real shell, file and desktop tools**. Do not
expose it directly to the internet. The server binds to loopback by default.
If you deploy it later:

- use a private network or an authenticated reverse proxy;
- sandbox the container and point `NEURONS_WORKSPACE` at only the files it needs;
- use `--no-shell` and `--no-screen` when the deployment does not need those;
- never put provider keys in the browser or in git.

`run_command` has guard rails against a few catastrophic patterns, but it is not
a sandbox. Everything else runs with the server user's privileges.

## Docker (local container)

```bash
docker build -t neurons .
docker run --rm -p 127.0.0.1:8770:8770 \
  -e NUMES_API_KEY=... \
  -v "$PWD/data:/app/data" neurons
```

The container command binds `0.0.0.0` internally so Docker can forward the
port. Publish it to `127.0.0.1` only, as above. A public deployment needs an
authentication layer first.
