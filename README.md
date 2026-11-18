# Neurons

A local-first AI harness with a real app shell. It streams reasoning and tool
calls, keeps a persistent profile and memory, saves conversations, connects to
the tools you already use, and can drive files, shell, browser, screen and
desktop through explicit tools.

The API keys stay on the server. The browser never receives them.

## What is in the app

- **Persistent memory** — profile, durable facts and every chat are stored in
  SQLite at `data/neurons.db` (configurable). A `remember` tool lets the model
  save lasting preferences; you can add or remove facts in the UI.
- **Personalization** — name, background/about, and response style are
  injected into every new conversation. The model can also remember details
  from earlier chats.
- **Saved conversation history** — chats appear in the sidebar and reload after
  a browser restart. Delete individual chats from the sidebar.
- **Switchable UI** — `graphite`, `aurora`, `ember`, `paper` and `terminal`
  themes, plus a custom PNG/JPEG/WebP/GIF backdrop with blur and dim controls.
- **Neuron Free** — when both Numes and Logfare keys are configured, the
  `neuron-free` provider presents one model catalogue and routes each selected
  model to the provider that owns it. Upstream balances, availability and fair
  use still apply; this is not a claim of unlimited inference.
- **Connections** — GitHub, Gmail, Google Calendar/Drive/Sheets, Notion,
  Outlook, Sigma, Slack, Discord, Linear, Jira, Airtable, Stripe, HubSpot,
  Salesforce, Asana, Dropbox, Supabase, Sentry, PagerDuty and more through one
  server-side token broker and one `plugin_call` tool.
- **Coding harnesses** — Claude Code, Codex, Antigravity, Freebuff, Hermes,
  Aider, OpenCode, Gemini CLI and Cursor Agent can be detected and launched as
  local one-shot processes. Their APIs are marked training-exempt.
- **Neuron Train (opt-in)** — an explicit local contribution ledger for
  eligible messages. It is off by default. This build does not upload, sell or
  pay out data; it provides a transparent export you can take to a future data
  partner. Claude Code, Codex, Antigravity and other protected harnesses are
  excluded.
- **Auto-updates** — `run.py` checks the published `VERSION` at launch and
  applies code-only updates while preserving `data/`, `.env` and `.agents/`.
- **Agent workspace** — files, shell, web, desktop control, skills, MCP servers,
  subagents and tool traces. Agent mode is on by default.

## Quick start

```bash
cd neurons
py -3.12 -m venv .venv
source .venv/Scripts/activate     # git-bash on Windows
pip install -r requirements.txt
cp .env.example .env              # fill in at least one provider
py run.py --open
```

Open <http://127.0.0.1:8770>. The marketing site is served at
<http://127.0.0.1:8770/site/>.

If the app is started from this folder, the file and shell tools default to
this folder as their workspace. To point them at a project, pass `--workspace`:

```bash
py run.py --open --workspace "C:/Users/Cameron/spirulina"
```

On Windows, `py -3.12` is recommended because the machine's bare `python` can
be older than the syntax used here.

## Automatic updates

Start with `run.py` to enable updates. On every launch it reads `VERSION` from
the repository, and if the remote version is newer it downloads that revision
before importing the server. A failed check never blocks startup.

Only application code is replaced: `server.py`, `store.py`, `updater.py`,
`run.py`, `plugins.py`, `harnesses.py`, `web/`, `site/`, `tests/`,
`requirements.txt`, `README.md`, `Dockerfile` and `VERSION`. **`data/`, `.env`,
`.env.*` and `.agents/` are never written by the updater.** Profile, memories,
chats, keys, plugin connections and installed skills survive an update.

```bash
py run.py --no-update   # start without checking
```

Set `NEURONS_UPDATE=off` to disable checks for a deployment, or
`NEURONS_UPDATE_BRANCH` to follow a branch other than `main`. Starting
`server.py` directly bypasses the launcher and its update check.

To publish an update, bump `VERSION` in the repository and push. Existing
launches pick it up the next time they start.

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
| `NEURONS_PROVIDER` | Provider selected at startup (`neuron-free` combines Numes + Logfare) |
| `NEURONS_FALLBACK` | Retry provider if the first fails before a tool runs |
| `NEURONS_WORKSPACE` | Root for file and shell tools (defaults to the app folder) |
| `NEURONS_DATA` | Data directory for SQLite, backgrounds and plugin broker |
| `NEURONS_ENV_FILE` | Extra `.env` file to load |
| `NEURONS_SKILLS` | Skills directory (defaults to `./.agents/skills`) |
| `NEURONS_MCP_CONFIG` | MCP server config (defaults to `./.mcp.json`) |
| `NEURONS_UPDATE` | `off` disables the startup update check |
| `NEURONS_UPDATE_BRANCH` | branch followed by the updater (defaults to `main`) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `FREEBUFF_API_KEY`, `CURSOR_API_KEY` | Optional credentials passed to installed coding harnesses |

Never commit `.env` or `data/`: both are ignored.

## Connections

Open **connections** in the header or sidebar. Pick a service, paste the
credential it asks for, and save. The credential is written to
`data/plugins.json` with user-only permissions (where the OS supports it), and
is never sent back to the browser. A connected service is exposed to the model
through the `plugin_call` tool, which is constrained to that service's base URL.

- **GitHub**: fine-grained personal access token.
- **Gmail / Google Calendar / Drive / Sheets**: an OAuth access token with the
  relevant scopes. Access tokens expire; reconnect with a fresh one when needed.
- **Notion**: internal integration token, shared with the pages you want it to
  read.
- **Outlook**: Microsoft Graph access token.
- **Sigma**: client ID, client secret and the API base URL for your Sigma
  region. Neurons exchanges the client credentials for a one-hour bearer token.
- **Jira**: site URL, account email and API token (Basic auth).
- **Salesforce / Supabase**: instance/project URL plus an access token/service key.
- **Slack / Discord / Linear / Stripe / HubSpot / Asana / Dropbox / Sentry /
  PagerDuty / Airtable**: service token or API key, as requested by the card.

This is a bring-your-own-credential integration layer, not a hosted OAuth
marketplace. Revoke access from the provider's own security settings and
**disconnect** here to clear the local copy.

## Coding harnesses

The connections drawer has a **harnesses** tab. Neurons checks `PATH` for each
known CLI and shows whether it is ready. When it is, you can give it a prompt
and a working directory; it runs as a one-shot local process and returns its
output. The adapters are deliberately fixed to known commands — this is not an
arbitrary shell bridge.

Each card has a key field. Paste the key the CLI needs (Anthropic for Claude
Code, OpenAI for Codex/Aider, Gemini for Antigravity/Gemini CLI, and so on) and
**save key**. It is stored in `data/harnesses.json` with user-only permissions
where the OS supports it, is used only for that harness process, and is never
sent back to the browser. A key already in `.env` is detected; saving one in
the UI overrides it for that harness. Use **remove key** to delete the local
copy.

A harness that is not installed still accepts a key, but cannot run until its
CLI is on `PATH`; the card shows the command (`claude`, `codex`, `antigravity`,
…) and a documentation link. Antigravity, Freebuff and Cursor Agent adapters are
labelled experimental because their CLI surfaces can change. The desktop app
does not silently install them.

These providers are marked **training-exempt**. They never enter the Neuron
Train ledger.

## Neuron Train and privacy

Neuron Train is **off by default**. When you turn it on in the profile drawer,
the server records a local row for eligible user/assistant messages: role,
length, estimated tokens, provider and timestamp. It does not transmit them
anywhere. **Export ledger** downloads a JSONL file; **clear ledger** removes the
local rows. Messages from protected coding harnesses are excluded.

There is no data sale, crypto payout or KYC flow in this build. Those require a
real buyer, a lawful data-rights framework, consent records and a payment
provider; shipping a fake "earnings" number would be dishonest. The export is
the portable primitive you can negotiate with later.

The marketing site is at `/site/` and is included in the application update.

## Memory model

`store.py` owns one SQLite database:

| Table | Contents |
|---|---|
| `profile` | name, about, response style, theme, background, training opt-in |
| `memories` | durable facts with source and timestamp |
| `conversations` | chat titles and timestamps |
| `messages` | user/assistant turns per conversation |
| `training_ledger` | local opt-in contribution accounting |

The server prepends the profile and memories to the model's system context on
every request. The client never supplies trusted memory; it only displays and
edits what the server returns. A `remember` call from the agent appends a fact,
and duplicate facts are collapsed case-insensitively.

## Provider reliability

Agent requests retry transient connection drops, timeouts, rate limits and
5xx responses up to three times. If a provider fails before any tool runs, the
fallback provider is tried. If tools already ran and the provider then drops,
Neurons shows the last completed tool result rather than re-running side
effects.

When a model calls `finish` with an empty answer twice, Neurons makes one final
request with tools removed and asks for plain prose. The UI also adds a
**retry** button to a failed bubble.

## UI

The app shell is in `web/`:

- `index.html` — sidebar, chat, connections, profile/memory, toolkit, themes
- `styles.css` — base chat and toolkit styles
- `app-extra.css` — original app shell, themes and responsive layout
- `app-v3.css` — the 2.3 backdrop, connections, appearance and training layer
- `app.js` — streaming agent/chat, queue and toolkit
- `app-shell.js` — saved chats, profile, memory, backgrounds, training, themes
- `plugins.js` — integration and harness drawer

The marketing site is in `site/` (`index.html`, `site.css`, `site.js`).

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | profile, training stats, memories, chats and active conversation |
| `POST /api/profile` | partial profile update, including background settings |
| `POST /api/background` | raw image/GIF upload, max 16 MB |
| `POST /api/background/delete` | remove the custom backdrop |
| `GET /api/background/file/<name>` | serve the local backdrop |
| `GET /api/plugins` | public catalog and connection status |
| `POST /api/plugins/connect` | save server-side credentials `{ id, fields }` |
| `POST /api/plugins/test` | call the integration's test endpoint |
| `POST /api/plugins/disconnect` | delete local credentials |
| `GET /api/harnesses` | detected local coding agents and key status |
| `POST /api/harnesses/key` | save or clear a harness key `{ id, key }` / `{ id, clear: true }` |
| `POST /api/harnesses/run` | run a one-shot local harness |
| `GET /api/training` | local contribution stats |
| `POST /api/training` | `{ enabled }` or `{ action: "clear" }` |
| `GET /api/training/export` | downloadable local JSONL ledger |
| `POST /api/memory` | add `{ "text": "..." }` |
| `POST /api/memory/delete` | delete `{ "id": 1 }` |
| `POST /api/conversations` | create a chat `{ "title": "..." }` |
| `GET /api/conversations?id=…` | load one chat and its messages |
| `POST /api/conversations/delete` | delete `{ "id": "..." }` |
| `POST /api/messages` | append `{ "conversation_id", "role", "content", "meta" }` |
| `GET /api/health` | liveness check and version |
| `GET /api/models`, `/api/providers` | model catalogue and provider routing |
| `GET /api/toolkit` | tools, MCP servers, skills and `plugin_call` |
| `POST /api/skills/install` | refresh the public skill catalog |
| `POST /api/chat`, `/api/agent`, `/api/agent/stream` | model calls |

## Tests

```bash
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

The tests cover profile validation, memory deduplication/deletion,
conversation ordering and cascade deletion, background settings, opt-in
training accounting and provider exemptions, plugin secret isolation and path
validation, harness metadata, and updater data preservation.

## Security

This is a local-first agent with **real shell, file and desktop tools**. Do not
expose it directly to the internet. The server binds to loopback by default.
If you deploy it later:

- use a private network or an authenticated reverse proxy;
- sandbox the container and point `NEURONS_WORKSPACE` at only the files it needs;
- use `--no-shell` and `--no-screen` when the deployment does not need those;
- never put provider keys in the browser or in git.

`run_command` has guard rails against a few catastrophic patterns, but it is not
a sandbox. Everything else runs with the server user's privileges. Plugin
and harness credentials are local secrets: protect `data/plugins.json` and
`data/harnesses.json`, and revoke tokens at the provider if a machine is lost.

## Docker (local container)

```bash
docker build -t neurons .
docker run --rm -p 127.0.0.1:8770:8770 \
  -e NUMES_API_KEY=... -e LOGFARE_API_KEY=... \
  -v "$PWD/data:/app/data" neurons
```

The container command binds `0.0.0.0` internally so Docker can forward the
port. It uses `run.py`, so container restarts also check for updates while the
`/app/data` volume preserves user data. Publish it to `127.0.0.1` only, as
above. A public deployment needs an authentication layer first.
