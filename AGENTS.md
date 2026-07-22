# AGENTS.md

Guidance for AI agents working on this repo.

## What this is

A self-hosted **agent org over chat** (persona via `BOT_NAME`): an orchestrator
session hires/briefs/supervises coder sessions, each visible in its own thread.
The core is **transport-agnostic**; Telegram is one adapter. See
[docs/architecture.md](docs/architecture.md) for the full picture.

## Architecture

```
transports/telegram.py  (adapter: topics ⇄ threads, header cards, commands)
        │  InboundMessage ▲ Outbound (via core.ports.Transport)
        ▼                 │
core/engine.py  Engine ── routes inbound, owns the fleet, exposes orchestrator
   ├─ core/session.py  CoreSession — one ClaudeSDKClient, posts via a callback
   ├─ core/store.py    thread registry + fleet records (restart-proof)
   └─ core/worktrees.py isolated git worktree per coder
```

- **`core/` imports no chat platform.** It speaks `Speaker`/`Outbound`/
  `InboundMessage` (`core/ports.py`). A transport implements `Transport`
  (create/close thread, `post`, busy) and calls `engine.on_inbound(...)`.
- **`CoreSession`** (`core/session.py`) — the old ClaudeSession, decoupled: all
  output goes through `post(Outbound(speaker=…))`; media tools are the `chat` MCP
  server (`mcp__chat__send_*`); `on_turn_done` hook fires the supervision wake; a
  `tap` lets the engine observe a coder thread.
- **`Engine`** (`core/engine.py`) — three session roles: `orchestrator` (fleet
  MCP tools: spawn/message/status/dismiss), `coder` (worktree cwd + STATUS
  protocol prompt), `direct` (the classic `/new`). The orchestrator lives in the
  transport's main thread ("general"). Coder turn-ends and human interjections
  land in `_inbox`; `_wake_orchestrator` coalesces them into one digest turn.
- **Identity**: one bot = one sender, so `TelegramTransport._header` prefixes a
  labelled header for orchestrator/coder speakers; direct sessions stay unadorned.

## Media flow

- **Inbound**: adapter `_collect_media` → `engine.on_inbound(InboundMessage)` →
  `CoreSession.submit_media`, which saves files under `<cwd>/.tg-inbox/` and
  queues a `Turn(text, images)`. Images ride as base64 image blocks.
- **Outbound**: session calls `mcp__chat__send_*`; `_tool_send` resolves the path
  **inside cwd** (refuses escapes) and emits an `Outbound` with `media_path`. 50 MB.

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
uv run boss
```

## Conventions

- **`core/` never imports a chat platform.** Platform code lives in `transports/`.
  Test the engine with a fake transport (see `tests/test_engine.py`).
- Persona is `BOT_NAME` (env), default `DEFAULT_BOT_NAME` in `__init__.py`.
- Prefer small pure functions in `rendering.py` for anything testable.
- Every git/subprocess call in `core/worktrees.py` is timeout-bounded — keep it so.
