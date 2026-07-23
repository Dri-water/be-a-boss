# Architecture

> The orchestrator is the default model. Direct (orchestrator-less) sessions via
> `/new` remain supported — they are layered underneath, not replaced.

## Principles

1. **Transport-agnostic core.** Everything that matters — sessions, the
   orchestrator, the fleet, supervision — lives in `core/` and speaks in its own
   vocabulary (`Thread`, `Speaker`, `OutboundEvent`). It never imports a chat
   platform. Telegram and a WebSocket surface (web app + VS Code) are adapters in
   `transports/`; Slack would be another, with zero core changes.
2. **Glass-walled delegation.** The orchestrator drives worker sessions, and every
   conversation happens in a visible thread. The human can watch any exchange and
   type into it as a third party — both agents see the interjection.
3. **One bot account, many identities.** Chat platforms bind one token to one
   sender, so speaker identity is rendered in the message (header card + thread
   name), not in the account. The core deals only in `Speaker` structs; how they
   render is the transport's job.
4. **Checkpoint supervision, not micro-management.** Workers get whole tasks and
   run autonomously (`bypassPermissions`). The orchestrator is woken at
   checkpoints — task finished, worker blocked, question asked, human interjected —
   never per token. Events come as SDK pushes, not polling.

## Layers

```mermaid
flowchart TB
    subgraph transports/
        TG[telegram adapter<br/>topics ⇄ threads, header cards]
        WS[websocket adapter<br/>web app + VS Code]
        SLACK[slack adapter<br/>next]
    end
    subgraph core/
        ENG[engine<br/>routes events, owns fleet]
        ORC[orchestrator<br/>one privileged session]
        FLEET[fleet<br/>worker registry + state]
        CS[CoreSession<br/>one per thread<br/>Claude / Codex backend]
        SUP[supervisor<br/>checkpoint inbox]
        ST[store<br/>restart-proof state]
    end
    TG -- InboundMessage --> ENG
    ENG -- OutboundEvent --> TG
    ENG --> ORC & FLEET & SUP
    FLEET --> CS
    SUP -- wake --> ORC
    ORC & CS --> ST
```

### The transport contract (`core/ports.py`)

A transport implements one small interface and receives one callback:

- `create_thread(title) -> thread_id` · `rename_thread` · `close_thread`
- `post(thread_id, speaker, content)` — content is text or media; the transport
  renders the speaker (headers, emojis, quoting) however fits the platform
- it calls `engine.on_inbound(InboundMessage)` for every human message

`Speaker` is `{role: orchestrator|worker|system, name, emoji}`. The core never
formats platform text; the adapter never holds session state.

## The org model

```mermaid
flowchart LR
    H([Human<br/>the boss]) <-->|main thread| O[🧭 Orchestrator<br/>persistent session]
    O <-->|"brief / report<br/>(visible in thread)"| C1[⚙️ worker Nova<br/>worktree A]
    O <-->|"brief / report"| C2[⚙️ worker Kite<br/>worktree B]
    H -.->|"interject in any<br/>worker thread"| C1
```

- **Main thread** (Telegram: the General topic) = the orchestrator's office. The
  human talks to the orchestrator here; fleet status lives here.
- **Worker thread** = one worker session + the orchestrator's side of that
  conversation. Everything either of them says is posted to the thread.
- **Interjection**: a human message in a worker thread is delivered to the worker
  as user input *and* recorded in the orchestrator's inbox, so both see it.
- The human can still run direct (orchestrator-less) sessions — the pre-existing
  `/new` flow is unchanged. The orchestrator is optional per thread.

## Session roles

| | orchestrator | worker | direct |
|---|---|---|---|
| Lifetime | persistent | per task | until `/kill` |
| cwd | none (fleet root) | git worktree of target repo | repo itself |
| Tools | fleet MCP tools (spawn/brief/status/…) | chat media tools | chat media tools |
| Speaks in | main thread + any worker thread | its own thread | its own thread |
| Supervised by | human | orchestrator (checkpoints) | human |

The orchestrator is itself a coding-agent session — its "powers" are MCP tools exposed
by the engine: `spawn_worker(repo, task, name?)`, `message_worker(id, text)`,
`worker_status(id?)`, `dismiss_worker(id)`, `report(text)`. Its system prompt
teaches briefing etiquette: self-contained briefs, explicit report-back markers,
escalate-don't-guess, and the code-quality bar it holds workers to.

## Supervision (checkpoint inbox)

The supervisor keeps an inbox per orchestrator. Producers:

- SDK events from worker sessions: turn ended (with result), error, question
  detected, budget/turn ceiling hit
- transport events: human interjection in a worker thread
- timers: a worker silent past its soft deadline

Benign events (tool chatter, streaming) are absorbed. Actionable events wake the
orchestrator with a digest turn: `[inbox] Nova: tests green, task complete` — the
orchestrator then decides: report to human, re-brief, dismiss, or spawn next.
Idle costs nothing — the orchestrator is only woken by real events (SDK pushes and
transport messages), never by polling.

## Worktree isolation

Every worker gets `git worktree add <fleet>/worktrees/<worker>-<slug>` on a fresh
branch `worker/<slug>`. The repo's primary checkout is never touched; parallel
workers on one repo can't collide. Teardown removes merged/clean worktrees and
reports dirty ones instead of deleting them.

## State (restart-proof)

`state/` holds JSON: thread registry (thread ⇄ role ⇄ session_id ⇄ cwd/worktree),
fleet records (worker id, name, task brief, status log), orchestrator session id.
On restart: threads reattach lazily (`resume=` on next message), the supervisor
rebuilds its inbox from unfinished worker records.

## Delivery (landing a worker's branch)

Work never dead-ends on a branch. `review_worker` returns a worker's committed diff
plus which routes are available; the orchestrator surfaces the change to the boss and,
**only on explicit approval**, calls `deliver_worker(method)`:

- **`merge`** — a deterministic local merge of `worker/<id>` into the primary
  checkout's current branch. Requires the checkout clean, aborts + rolls back on
  conflict, never force-anything. The one irreversible step is boring, gated code —
  not the LLM freehanding git.
- **`pr`** — pushes the branch and opens a GitHub PR (`gh pr create`). Non-destructive,
  so it's fine for the agent path; available only when a remote **and** an authenticated
  `gh` exist, else it degrades to a local merge.

The split is deliberate: **capability is detected, policy is the orchestrator's, the
boss is the gate.** `gh` auth presence *is* the config — no delivery-mode flag. Deeper
policy (ship/scout task types, required reviewers) can layer on later; the core loop
now lands work end to end.
