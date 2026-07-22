# AGENTS.md

Guidance for AI agents working on this repo.

## What this is

A self-hosted Telegram bot (display persona configurable via `BOT_NAME`) that
manages **parallel Claude Code sessions — one live session per Telegram forum
topic**. No orchestrator: a message in a topic is that Claude session's next turn.

## Architecture (one direction of data flow)

```
Telegram topic ──msg──► on_message ──► SessionManager.route
                                          │
                                          ▼
                                   ClaudeSession (per topic)
                                     queue → worker → ClaudeSDKClient
                                          │
Telegram topic ◄── TelegramEmitter ◄── rendering ◄── SDK messages
```

- `claude_session.py` — the crux. One long-lived `ClaudeSDKClient` per topic,
  `permission_mode=bypassPermissions`, `cwd=<repo>`, turns serialized via an
  `asyncio.Queue[Turn]`. Captures `session_id` (from `SystemMessage.init` /
  `ResultMessage`) and persists it so restarts `resume=` the same session.
  Also builds a **per-session in-process MCP server** ("telegram") exposing
  `send_photo/send_video/send_file/send_message`, wired to this topic's emitter.
- `manager.py` — creates sessions, **lazily resumes** dormant ones on first
  message, `route`/`route_media`/kills/interrupts.
- `store.py` — JSON map `thread_id -> {cwd, session_id, name}`.
- `rendering.py` — pure `SDK message -> list[str]`. Plain text only (no
  parse_mode) to dodge Telegram entity-parse 400s. Tests live against these.
- `telegram_bot.py` — PTB v22 handlers + `TelegramEmitter` (text + photo/video/
  document). `on_media` downloads attachments into `MediaItem`s and routes them.

## Media flow

- **Inbound**: `on_media` → `_collect_media` (download each attachment) →
  `manager.route_media` → `ClaudeSession.submit_media`, which writes files to
  `<cwd>/.tg-inbox/` and queues a `Turn(text, images)`. Images ride along as
  `{"type":"image","source":{base64}}` blocks via `client.query(async_iterable)`.
- **Outbound**: the session calls `mcp__telegram__send_*`; `_tool_send` resolves
  the path **inside cwd** (refuses escapes), then calls the emitter. 50 MB cap.

## Invariants / gotchas

- **Never add `parse_mode`** to session output without escaping — Claude output
  breaks Markdown/HTML parsing constantly.
- **Running as root needs `IS_SANDBOX=1`** (set in the Dockerfile). Claude Code
  refuses `--dangerously-skip-permissions` under root without it — sessions won't
  even start. The boot check won't catch this (sessions only spawn on `/new`);
  the in-container end-to-end test does.
- The SDK drives the **standalone** `claude` CLI (`npm i -g @anthropic-ai/claude-code`),
  not the VS Code extension. Auth is shared via `~/.claude/.credentials.json`.
- `bypassPermissions` is required: headless sessions have no TTY to answer prompts.
- Confirm SDK field/method names against the **installed** `claude-agent-sdk`
  (`uv run python -c "import claude_agent_sdk, dataclasses; ..."`), not memory —
  the API moves. (e.g. MCP tools use `@tool` + `create_sdk_mcp_server`.)
- One event loop (PTB's). Sessions create tasks on it; don't spin up your own loop.

## Run

```
uv sync
cp .env.example .env   # fill TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS
uv run tasm
```

## Conventions

- Keep the persona name in `__init__.py::BOT_NAME`; the repo name stays generic.
- Prefer small pure functions in `rendering.py` for anything testable.
