# Extending

be-a-boss has exactly two seams. Keeping it to two — and keeping the core ignorant
of both — is what makes it extensible without becoming a plugin swamp.

## Seam 1 — the surface (transport)

**Intent:** let you drive the same org from anywhere — a chat app, a browser, an
editor — without the core knowing which.

A surface adapter does three things:

- **carries threads** — it can open, name, and close a conversation, and it maps
  its native container (a Telegram topic, a web tab, a Slack thread) onto the
  core's idea of a "thread";
- **shows what the core says** — it renders an outgoing message or file, attributed
  to a *speaker* (orchestrator / worker / system). How identity is shown is the
  surface's call (a header line, a username, an avatar);
- **feeds in what you say** — it turns your messages into the core's inbound form
  and hands them over.

That's the whole contract. The core never formats platform text; the adapter never
holds session state. A new surface is a new adapter and **zero core changes**.

- **Supported:** Telegram; a **WebSocket** surface (the bundled web app,
  `python -m beaboss.web`); and a **CLI** (`boss-cli`) with an agent-drivable
  `--json` mode and a Textual cockpit — all speaking the same tiny event protocol.
- **Next:** Slack.

## Seam 2 — the agent backend

**Intent:** let a worker run on whatever coding agent you prefer, without the org
logic caring which.

A backend is *what a worker actually is*: something you can start in a working
directory, send a turn to, stream results back from, interrupt, and stop — plus a
way to resume it later. Claude Code is one such backend; Codex is another. The
orchestrator, supervision, and isolation don't change when you swap it — a worker is
a worker.

- **Supported:** Claude Code (default) and **Codex** (`BEABOSS_BACKEND=codex`).
  Codex runs via its `codex exec` CLI, translated into the same event vocabulary —
  the orchestrator, supervision, and isolation don't change.

## The rule that keeps this honest

If adding a surface or a backend ever requires touching the core, the seam is in the
wrong place — fix the seam, don't thread a special case through the middle. Two
small, well-placed seams beat a dozen configuration knobs.
