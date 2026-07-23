# Behaviour

What be-a-boss actually does, in the situations that matter. This is the contract
you can rely on; the code is where the mechanics live.

## You send a message

- **In the main thread** → it goes to the orchestrator as its next turn. The first
  message here brings the orchestrator to life. (Only the main thread does this — a
  stray message in some other thread never accidentally becomes the office.)
- **In a worker thread** → see [interjection](#you-interject-in-a-worker-thread).
- **In a direct thread** → it goes straight to that agent.
- **In an unknown thread** → you get a short note telling you where to talk.

Text, photos, and files are all accepted. Images are given to the agent as vision;
other files are saved into the workspace and referenced.

## The orchestrator hires a worker

When the orchestrator decides a task needs doing, it hires a worker. That, as one
step:

1. a new worker thread appears (named for the worker + repo);
2. the worker gets an isolated worktree on a fresh branch;
3. the orchestrator's brief is posted into the thread and handed to the worker;
4. the worker starts working, autonomously, toward the brief's definition of done.

If the workspace can't be set up (e.g. the target isn't a git repo), nothing is
half-created — you get one clear, actionable message instead.

## You interject in a worker thread

Your message is delivered to the worker as input **and** recorded for the
orchestrator. Both see it. The worker treats your word as authoritative; the
orchestrator stays aware of what you told them. You don't have to go through the
manager to steer someone — but the manager still knows what happened.

## Supervision — how the orchestrator stays in the loop

The orchestrator is **event-driven, never polling**. It's woken only when something
warrants it: a worker finished a turn, got blocked, needs a decision, or you
interjected. Routine progress doesn't wake it. Near-simultaneous events are
coalesced into a single wake so it reacts once, not five times. Idle costs nothing.

A worker ends every turn with an honest one-line status — `done`, `working`,
`blocked: <what it needs>`, or `needs-decision: <the options>`. `blocked` means the
orchestrator should help; `needs-decision` is escalated to you with a recommendation.

## A worker finishes — and the work gets delivered

Finished work lives on the worker's branch; it outlives the worker. But it doesn't
dead-end there. When a task is done, the orchestrator shows you the actual **diff**
and the **real test result** (it runs your checks in the worker's copy — not "the
worker says it passes"), then asks you to land it. Delivery is a **command you
issue**: the orchestrator posts a `🚦` prompt and only your **`/approve <worker>`**
actually merges or opens the PR — it can request, never authorize. Work whose checks
failed can't be delivered at all until they're green. The route is either a **local
merge** into the branch the worker forked from (deterministic; refuses a dirty or
wrong-branch checkout; rolls back a conflict) or, if you've set up a GitHub remote +
`gh`, a **pull request** you review and merge. You are always the gate; the
orchestrator just picks the route from what your box can actually do (no `gh` → local
merge). Nothing lands without your word.

Dismissing a worker then cleans up its workspace **only if the work is committed**; a
worker with un-committed changes is never quietly discarded — the tool refuses and
tells you, so you decide.

## Errors

Failures are meant to be *legible*. A bad configuration, a missing tool, a git
failure — each produces a plain message that says what went wrong and what to do
about it, not a stack trace. If you ever see a raw traceback reach the chat, that's
a bug.

## Restarts

State is durable. If the process restarts, threads reattach and sessions resume
with their full context on the next message — you don't lose your org.

## Identity with one account

A single bot account can't change its sender per message, so each speaker is shown
with a header (🧭 orchestrator, ⚙️ worker) and the thread carries the worker's name.
Three participants, one account, no confusion.
