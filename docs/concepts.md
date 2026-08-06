# Concepts

be-a-boss models a tiny software organisation. That framing is the whole point:
it's a mental model everyone already has, so the tool is obvious to use.

## The roles

- **You — the boss.** You set goals and make the calls that are yours to make. You
  don't manage the details; you have someone for that.
- **The orchestrator — your portfolio lead.** One persistent agent you talk to. It
  keeps your priorities straight across projects, delegates, and reports outcomes.
  It does *not* write project code itself — it directs.
- **Project managers — repo specialists.** At most one for each canonical repo.
  A manager remembers that project's plan and decisions, briefs and supervises its
  workers, and escalates the parts that need you or the orchestrator. Managers are
  used adaptively: a quick one-off task can skip this layer.
- **Workers — the individual contributors.** Short-lived agents, one per task. Each
  gets a clean, isolated copy of the repo, does the work, commits it, and reports a
  status. When the task is done, the worker is let go.

## The glass wall

The defining idea: **you can see and join every conversation.** Each project manager
and worker gets a visible room. You can read any of them and speak at the right
altitude: portfolio direction with the orchestrator, project decisions with the
manager, or a precise interjection with a worker. The worker and its supervisor see
that interjection, like walking up to someone's desk. Nothing happens in a black box.

## Threads

A **thread** (or room) is one conversation. There are four kinds:

- the **orchestrator thread** — where you talk to the one orchestrator. Reach it in
  the shared group thread or by DM; both drive the same orchestrator, which replies
  wherever you spoke.
- a **project room** — the visible home of one repo and its manager;
- a **worker room** — one worker being directed (the glass wall above);
- a **direct thread** — you talking straight to a single agent, no orchestrator in
  the middle. For quick, hands-on work where a manager would just be overhead.

How a thread shows up depends on the **surface**: on Telegram a group thread is a
forum topic, and a DM is the private chat itself. The core doesn't know or care. (On
Telegram, which cannot nest topics, project and worker topics carry the project in
their names and headers. The shared `#general` also carries a live, code-rendered
portfolio board.)

## Adaptive, not automatic overhead

A hierarchy helps when several unrelated repos would otherwise compete for one
conversation's finite context. It can hurt a small sequential task by adding extra
messages and another opportunity for a hand-off to lose detail. be-a-boss therefore
keeps both routes:

```text
orchestrator → project manager → worker   long-lived or multi-project work
orchestrator → worker                     small, direct delegation
```

The hierarchy is deliberately shallow. A project manager cannot create another
manager, cannot operate outside its canonical repo, and cannot deliver work. It can
review and request delivery; the orchestrator and your configured approval policy
remain the authority for landing a branch.

A manager is also a persistent *identity*, not an always-running process. Its
project record and backend session ID survive while the process hibernates between
turns. A new message or worker checkpoint resumes it naturally. After a restart,
stored manager, worker, git, and session state reconstruct what needs attention, so
hibernation and reboot do not abandon work.

## Isolation

Workers never share a workspace. Each gets its own git worktree on its own branch,
so two workers on the same repo can't step on each other, and nothing a worker does
touches your main checkout until you choose to merge it. Un-merged work is never
thrown away.

## Two seams, everything else is core

Only two things are pluggable, on purpose:

- the **surface** — how you drive it (Telegram, web, and CLI/TUI today; the protocol is UI-agnostic);
- the **agent backend** — what every session runs (Claude Code and Codex today).

Everything in between — the org logic, supervision, isolation — is one small core
that knows about neither. Keeping the seams few and the core simple is what keeps
the whole thing observable and easy to extend. See [extending.md](extending.md).
