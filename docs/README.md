# be-a-boss docs

These docs describe **intent and behaviour** — what be-a-boss is for and how it
acts. They are deliberately not a code walkthrough: the code is the source of
truth for *how*, and is kept simple enough to read directly.

- **[concepts.md](concepts.md)** — the mental model: boss, orchestrator, worker,
  thread, the glass wall, and why it's shaped this way.
- **[behaviour.md](behaviour.md)** — what actually happens: message routing,
  hiring/briefing a worker, interjection, supervision, dismissal, errors, restarts.
- **[extending.md](extending.md)** — the two seams: adding a **surface**
  (transport) or an **agent backend**. Contracts and intent, not implementation.
- **[architecture.md](architecture.md)** — the layered design + diagrams.

## Design values

Everything here is built to one bar: **robustness through simplicity.** Prefer the
simplest thing that works; build for extension and modularity with clear seams; and
refuse to over-engineer. Simple code is more observable, more robust, and easier to
maintain — and it's the standard the orchestrator holds its workers to, too.
