# Concepts

be-a-boss models a tiny software organisation. That framing is the whole point:
it's a mental model everyone already has, so the tool is obvious to use.

## The roles

- **You — the boss.** You set goals and make the calls that are yours to make. You
  don't manage the details; you have someone for that.
- **The orchestrator — your manager.** One persistent agent you talk to. It breaks
  your goals into tasks, hires workers, briefs them, supervises, and reports
  outcomes back to you. It does *not* write project code itself — it directs.
- **Workers — the individual contributors.** Short-lived agents, one per task. Each
  gets a clean, isolated copy of the repo, does the work, commits it, and reports a
  status. When the task is done, the worker is let go.

## The glass wall

The defining idea: **you can see and join every conversation.** Each worker gets its
own thread where the orchestrator's instructions and the worker's work both appear,
live. You can read any of them, and you can type into any of them — your message
reaches the worker *and* the orchestrator, like walking up to someone's desk. This
is what makes delegation trustworthy: nothing happens in a black box.

## Threads

A **thread** is one conversation. There are three kinds:

- the **orchestrator thread** — where you talk to the one orchestrator. Reach it in
  the shared group thread or by DM; both drive the same orchestrator, which replies
  wherever you spoke.
- a **worker thread** — one worker being directed (the glass wall above);
- a **direct thread** — you talking straight to a single agent, no orchestrator in
  the middle. For quick, hands-on work where a manager would just be overhead.

How a thread shows up depends on the **surface**: on Telegram a group thread is a
forum topic, and a DM is the private chat itself. The core doesn't know or care. (On
Telegram, the shared `#general` also carries a live, code-rendered status board.)

## Isolation

Workers never share a workspace. Each gets its own git worktree on its own branch,
so two workers on the same repo can't step on each other, and nothing a worker does
touches your main checkout until you choose to merge it. Un-merged work is never
thrown away.

## Two seams, everything else is core

Only two things are pluggable, on purpose:

- the **surface** — how you drive it (Telegram, web, and VS Code today; Slack next);
- the **agent backend** — what a worker actually runs (Claude Code and Codex today).

Everything in between — the org logic, supervision, isolation — is one small core
that knows about neither. Keeping the seams few and the core simple is what keeps
the whole thing observable and easy to extend. See [extending.md](extending.md).
